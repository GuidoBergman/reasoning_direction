import torch
import os

from typing import List
from jaxtyping import Float
from torch import Tensor
from tqdm import tqdm

from pipeline.utils.hook_utils import add_hooks
from pipeline.model_utils.model_base import ModelBase

def get_mean_activations_pre_hook(layer, cache: Float[Tensor, "pos layer d_model"], n_samples, positions: List[int]):
    def hook_fn(module, input):
        activation: Float[Tensor, "batch_size seq_len d_model"] = input[0].clone().to(cache)
        cache[:, layer] += (1.0 / n_samples) * activation[:, positions, :].sum(dim=0)
    return hook_fn

def get_mean_activations(model, tokenizer, instructions ,tokenize_instructions_fn, block_modules: List[torch.nn.Module], batch_size=1, positions=[-1]):
    torch.cuda.empty_cache()

    n_positions = len(positions)
    n_layers = model.config.num_hidden_layers
    n_samples = len(instructions)
    d_model = model.config.hidden_size

    # we store the mean activations in high-precision to avoid numerical issues
    mean_activations = torch.zeros((n_positions, n_layers, d_model), dtype=torch.float64, device=model.device)

    fwd_pre_hooks = [(block_modules[layer], get_mean_activations_pre_hook(layer=layer, cache=mean_activations, n_samples=n_samples, positions=positions)) for layer in range(n_layers)]

    for i in tqdm(range(0, len(instructions), batch_size)):
        questions = [instruction['question'] for instruction in instructions[i:i+batch_size]]
        prompts = [instruction['prompt'] for instruction in instructions[i:i+batch_size]]
        first_responses = [instruction['first_response'] for instruction in instructions[i:i+batch_size]]
        inputs = tokenize_instructions_fn(questions=questions, prompts=prompts, first_responses=first_responses)


        with add_hooks(module_forward_pre_hooks=fwd_pre_hooks, module_forward_hooks=[]):
            model(
                input_ids=inputs.to(model.device)
            )

    return mean_activations

def get_mean_diff(model, tokenizer, train_correct, train_incorrect, tokenize_instructions_fn, block_modules: List[torch.nn.Module], batch_size=1, positions=[-1]):
    mean_activations_correct = get_mean_activations(model, tokenizer, train_correct, tokenize_instructions_fn, block_modules, batch_size=batch_size, positions=positions)
    mean_activations_incorrect = get_mean_activations(model, tokenizer, train_incorrect, tokenize_instructions_fn, block_modules, batch_size=batch_size, positions=positions)

    mean_diff: Float[Tensor, "n_positions n_layers d_model"] = mean_activations_correct - mean_activations_incorrect

    return mean_diff

def generate_directions(model_base: ModelBase, train_correct, train_incorrect, artifact_dir):
    if not os.path.exists(artifact_dir):
        os.makedirs(artifact_dir)

    mean_diffs = get_mean_diff(model_base.model, model_base.tokenizer, train_correct, train_incorrect, model_base.tokenize_instructions_fn, model_base.model_block_modules, positions=list(range(-len(model_base.eoi_toks), 0)))

    assert mean_diffs.shape == (len(model_base.eoi_toks), model_base.model.config.num_hidden_layers, model_base.model.config.hidden_size)
    assert not mean_diffs.isnan().any()

    torch.save(mean_diffs, f"{artifact_dir}/mean_diffs.pt")

    return mean_diffs