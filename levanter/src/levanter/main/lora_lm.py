import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import jax.random as jrandom

import haliax.random

import levanter
from levanter import callbacks
from levanter.compat.hf_checkpoints import HFCheckpointConverter
from levanter.data.text import CausalLmDataset, LMDatasetConfig
from levanter.lora import (
    LoraConfig,
    lora_trainable_params_filter,
    loraize,
    save_merged_hf_checkpoint_callback,
    save_peft_checkpoint_callback,
)
from levanter.optim import AdamConfig, OptimizerConfig
from levanter.trainer import Trainer, TrainerConfig
from levanter.utils.jax_utils import parameter_count
from levanter.utils.py_utils import non_caching_cycle


logger = logging.getLogger(__name__)


@dataclass
class LoraLmConfig:
    initialize_from_hf: str
    lora: LoraConfig = field(default_factory=LoraConfig)
    data: LMDatasetConfig = field(default_factory=LMDatasetConfig)
    trainer: TrainerConfig = field(default_factory=TrainerConfig)
    optimizer: OptimizerConfig = field(default_factory=AdamConfig)

    peft_save_path: Optional[str] = None  # path to save peft-compatible checkpoints
    peft_hf_upload: Optional[str] = None
    hf_save_steps: int = 1000

    merged_hf_save_path: Optional[str] = None  # path to save merged hf checkpoints
    merged_hf_upload: Optional[str] = None

    trust_remote_code: bool = False
    max_train_length: Optional[int] = None  # if set, train on sequences of this length


def main(config: LoraLmConfig):
    levanter.initialize(config)
    tokenizer = config.data.the_tokenizer

    converter = HFCheckpointConverter.from_hf(config.initialize_from_hf, trust_remote_code=config.trust_remote_code)
    if tokenizer.vocab != converter.tokenizer.vocab:
        logger.warning("The tokenizers appear to be different. You may want to check this.")

    converter = converter.replaced(tokenizer=tokenizer)

    model_config = converter.default_config

    # randomness in jax is tightly controlled by "keys" which are the states of the random number generators
    # this makes deterministic training pretty easy
    seed = config.trainer.seed
    data_key, loader_key, model_key, lora_key, training_key = jrandom.split(jrandom.PRNGKey(seed), 5)

    # some axes we need
    Batch = config.trainer.TrainBatch
    Pos = model_config.Pos
    KeyPos = model_config.KeyPos

    if config.max_train_length is not None:
        logger.info(f"Setting max tune length to {config.max_train_length}")
        Pos = Pos.resize(config.max_train_length)
        KeyPos = KeyPos.resize(config.max_train_length)

    optimizer = config.optimizer.build(config.trainer.num_train_steps)

    with Trainer(config.trainer, optimizer) as trainer:
        # how we shard parameters across devices
        parameter_axis_mapping = config.trainer.parameter_axis_mapping

        eval_datasets = config.data.validation_sets(Pos.size)

        # data loaders
        if len(eval_datasets) == 0:
            logger.warning("No evaluation datasets provided.")

        train_dataset = CausalLmDataset(config.data.train_set(Pos.size, key=data_key), Pos, KeyPos)
        train_loader = trainer.sharded_loader(train_dataset, Batch)

        # load the underlying hf model
        logger.info(f"Loading pretrained model from {converter.reference_checkpoint}")
        model = converter.load_pretrained(
            model_config.model_type, model_config, axis_mapping=parameter_axis_mapping, dtype=trainer.mp.compute_dtype
        )

        @haliax.named_jit(axis_resources=parameter_axis_mapping, donate_args=(True))
        def loraize_hf_model(model):
            return loraize(model, config.lora, key=lora_key)

        model = loraize_hf_model(model)

        lora_param_filter = lora_trainable_params_filter(model)

        state = trainer.initial_state(training_key, model=model, is_trainable=lora_param_filter)

        all_param_count = parameter_count(state.model)
        # TODO: remove this once we put this in trainer itself
        just_lora_params = parameter_count(state.trainable_model)

        levanter.tracker.log_summary(
            {
                "parameter_count": all_param_count,
                "trainable_parameter_count": just_lora_params,
                "fraction_trainable": just_lora_params * 1.0 / all_param_count,
            }
        )

        logger.info(f"Total parameter count: {all_param_count}")
        logger.info(f"Trainable parameter count: {just_lora_params}")
        logger.info(f"Fraction of parameters that are trainable: {just_lora_params * 1.0 / all_param_count:.3e}")

        for name, eval_dataset in eval_datasets.items():
            eval_dataset = CausalLmDataset(eval_dataset, Pos, KeyPos)
            trainer.add_eval_hook(eval_dataset, name=name)

        # boilerplate hooks and such
        if len(eval_datasets) == 0:
            logger.warning("No evaluation datasets provided.")

        for name, eval_dataset in eval_datasets.items():
            eval_dataset = CausalLmDataset(eval_dataset, Pos, KeyPos, ignore_index=config.data.ignore_token_id)
            trainer.add_eval_hook(eval_dataset, name=name)

        trainer.add_hook(callbacks.log_performance_stats(Pos.size, trainer.config.train_batch_size), every=1)
        if config.peft_save_path is not None:
            full_save_path = os.path.join(config.peft_save_path, trainer.run_id)
            trainer.add_hook(
                save_peft_checkpoint_callback(
                    full_save_path, config.lora, config.initialize_from_hf, tokenizer, config.peft_hf_upload
                ),
                every=config.hf_save_steps,
            )

        if config.merged_hf_save_path is not None:
            full_save_path = os.path.join(config.merged_hf_save_path, trainer.run_id)
            trainer.add_hook(
                save_merged_hf_checkpoint_callback(full_save_path, converter, config.merged_hf_upload),
                every=config.hf_save_steps,
            )

        # data loader. may need to seek to the right place if we're resuming
        iter_data = non_caching_cycle(train_loader)

        if int(state.step) > 0:
            # step is after the batch, so we need to seek to step
            # TODO: implement iter_data.seek(resume_step +1)
            import tqdm

            for _ in tqdm.tqdm(range(state.step), desc="seeking data for resume"):
                next(iter_data)

        ## OK, actually run training!
        trainer.train(state, iter_data)


if __name__ == "__main__":
    levanter.config.main(main)()
