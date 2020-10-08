import logging
import os
import time

from ray.rllib.agents.trainer import Trainer, COMMON_CONFIG
from ray.rllib.optimizers import SyncSamplesOptimizer
from ray.rllib.utils import add_mixins
from ray.rllib.utils.annotations import override, DeveloperAPI
import numpy as np
from zlib import compress, decompress
from sys import getsizeof


logger = logging.getLogger(__name__)


@DeveloperAPI
def build_trainer(name,
                  default_policy,
                  default_config=None,
                  validate_config=None,
                  get_initial_state=None,
                  get_policy_class=None,
                  before_init=None,
                  make_workers=None,
                  make_policy_optimizer=None,
                  after_init=None,
                  before_train_step=None,
                  after_optimizer_step=None,
                  after_train_result=None,
                  collect_metrics_fn=None,
                  before_evaluate_fn=None,
                  mixins=None,
                  execution_plan=None):
    """Helper function for defining a custom trainer.

    Functions will be run in this order to initialize the trainer:
        1. Config setup: validate_config, get_initial_state, get_policy
        2. Worker setup: before_init, make_workers, make_policy_optimizer
        3. Post setup: after_init

    Arguments:
        name (str): name of the trainer (e.g., "PPO")
        default_policy (cls): the default Policy class to use
        default_config (dict): The default config dict of the algorithm,
            otherwise uses the Trainer default config.
        validate_config (func): optional callback that checks a given config
            for correctness. It may mutate the config as needed.
        get_initial_state (func): optional function that returns the initial
            state dict given the trainer instance as an argument. The state
            dict must be serializable so that it can be checkpointed, and will
            be available as the `trainer.state` variable.
        get_policy_class (func): optional callback that takes a config and
            returns the policy class to override the default with
        before_init (func): optional function to run at the start of trainer
            init that takes the trainer instance as argument
        make_workers (func): override the method that creates rollout workers.
            This takes in (trainer, env_creator, policy, config) as args.
        make_policy_optimizer (func): optional function that returns a
            PolicyOptimizer instance given (WorkerSet, config)
        after_init (func): optional function to run at the end of trainer init
            that takes the trainer instance as argument
        before_train_step (func): optional callback to run before each train()
            call. It takes the trainer instance as an argument.
        after_optimizer_step (func): optional callback to run after each
            step() call to the policy optimizer. It takes the trainer instance
            and the policy gradient fetches as arguments.
        after_train_result (func): optional callback to run at the end of each
            train() call. It takes the trainer instance and result dict as
            arguments, and may mutate the result dict as needed.
        collect_metrics_fn (func): override the method used to collect metrics.
            It takes the trainer instance as argumnt.
        before_evaluate_fn (func): callback to run before evaluation. This
            takes the trainer instance as argument.
        mixins (list): list of any class mixins for the returned trainer class.
            These mixins will be applied in order and will have higher
            precedence than the Trainer class
        execution_plan (func): Experimental distributed execution
            API. This overrides `make_policy_optimizer`.

    Returns:
        a Trainer instance that uses the specified args.
    """

    original_kwargs = locals().copy()
    base = add_mixins(Trainer, mixins)

    class trainer_cls(base):
        _name = name
        _default_config = default_config or COMMON_CONFIG
        _policy = default_policy

        def __init__(self, config=None, env=None, logger_creator=None):
            Trainer.__init__(self, config, env, logger_creator)

        def _init(self, config, env_creator):
            if validate_config:
                validate_config(config)

            if get_initial_state:
                self.state = get_initial_state(self)
            else:
                self.state = {}
            if get_policy_class is None:
                self._policy = default_policy
            else:
                self._policy = get_policy_class(config)
            if before_init:
                before_init(self)
            use_exec_api = (execution_plan
                            and (self.config["use_exec_api"]
                                 or "RLLIB_EXEC_API" in os.environ))

            # Creating all workers (excluding evaluation workers).
            if make_workers and not use_exec_api:
                self.workers = make_workers(self, env_creator, self._policy,
                                            config)
            else:
                self.workers = self._make_workers(env_creator, self._policy,
                                                  config,
                                                  self.config["num_workers"])
            self.train_exec_impl = None
            self.optimizer = None
            self.execution_plan = execution_plan

            if use_exec_api:
                logger.warning(
                    "The experimental distributed execution API is enabled "
                    "for this algorithm. Disable this by setting "
                    "'use_exec_api': False.")
                self.train_exec_impl = execution_plan(self.workers, config)
            elif make_policy_optimizer:
                self.optimizer = make_policy_optimizer(self.workers, config)
            else:
                optimizer_config = dict(
                    config["optimizer"],
                    **{"train_batch_size": config["train_batch_size"]})
                self.optimizer = SyncSamplesOptimizer(self.workers,
                                                      **optimizer_config)
            if after_init:
                after_init(self)

        @override(Trainer)
        def _train(self):
            if self.train_exec_impl:
                return self._train_exec_impl()

            if before_train_step:
                before_train_step(self)
            prev_steps = self.optimizer.num_steps_sampled

            start = time.time()
            optimizer_steps_this_iter = 0
            while True:
                fetches = self.optimizer.step()
                optimizer_steps_this_iter += 1
                if after_optimizer_step:
                    after_optimizer_step(self, fetches)
                if (time.time() - start >= self.config["min_iter_time_s"]
                        and self.optimizer.num_steps_sampled - prev_steps >=
                        self.config["timesteps_per_iteration"]):
                    break

            if collect_metrics_fn:
                res = collect_metrics_fn(self)
            else:
                res = self.collect_metrics()
            res.update(
                optimizer_steps_this_iter=optimizer_steps_this_iter,
                timesteps_this_iter=self.optimizer.num_steps_sampled -
                prev_steps,
                info=res.get("info", {}))

            if after_train_result:
                after_train_result(self, res)
            return res

        def _train_exec_impl(self):
            if before_train_step:
                logger.debug("Ignoring before_train_step callback")
            res = next(self.train_exec_impl)
            if after_train_result:
                logger.debug("Ignoring after_train_result callback")
            return res

        @override(Trainer)
        def _before_evaluate(self):
            if before_evaluate_fn:
                before_evaluate_fn(self)

        def __getstate__(self):
            state = Trainer.__getstate__(self)
            state["trainer_state"] = self.state.copy()
            policy = Trainer.get_policy(self)
            state["custom_state_vars"] = policy.get_custom_state_vars()
            state["optimizer_state"] = {k: v for k, v in policy.optimizer.state_dict().items()}
            
            ## Ugly hack to save replay buffer because organizers taking forever to give fix for spot instances
            save_success = False
            max_size = 3_700_000_000
            if policy.exp_replay.nbytes < max_size:
                state["replay_buffer"] = policy.exp_replay
                state["buffer_saved"] = 1
                policy.save_success  = 1
                save_success = True
            elif policy.exp_replay.shape[-1] == 6: # only for frame stack = 2
                eq = np.all(policy.exp_replay[1:,...,:3] == policy.exp_replay[:-1,...,-3:], axis=(-3,-2,-1))
                non_eq = np.where(1 - eq)
                images_non_eq = policy.exp_replay[non_eq]
                images_last = policy.exp_replay[-1,...,-3:]
                images_first = policy.exp_replay[0,...,:3]
                if policy.exp_replay[1:,...,:3].nbytes < max_size:
                    state["sliced_buffer"] = policy.exp_replay[1:,...,:3]
                    state["buffer_saved"] = 2
                    policy.save_success  = 2
                    save_success = True
                else:
                    comp = compress(policy.exp_replay[1:,...,:3].copy(), level=9)
                    if getsizeof(comp) < max_size:
                        state["compressed_buffer"] = comp
                        state["buffer_saved"] = 3
                        policy.save_success  = 3
                        save_success = True
                if save_success:
                    state["matched_frame_data"] = [non_eq, images_non_eq, images_last, images_first]
            
            if not save_success:   
                state["buffer_saved"] = -1
                policy.save_success  = -1
                print("####################### BUFFER SAVE FAILED #########################")
            else:
                state["retune_selector"] = policy.retune_selector
                
            
            if self.train_exec_impl:
                state["train_exec_impl"] = (
                    self.train_exec_impl.shared_metrics.get().save())
            return state

        def __setstate__(self, state):
            Trainer.__setstate__(self, state)
            policy = Trainer.get_policy(self)
            self.state = state["trainer_state"].copy()
            policy.set_optimizer_state(state["optimizer_state"])
            policy.set_custom_state_vars(state["custom_state_vars"])

            ## Ugly hack to save replay buffer because organizers taking forever to give fix for spot instances
            buffer_saved = state.get("buffer_saved", -1)
            policy.save_success  = buffer_saved
            if buffer_saved == 1:
                policy.exp_replay = state["replay_buffer"]
            elif buffer_saved > 1:
                non_eq, images_non_eq, images_last, images_first = state["matched_frame_data"]
                policy.exp_replay[non_eq] = images_non_eq
                policy.exp_replay[-1,...,-3:] = images_last
                policy.exp_replay[0,...,:3] = images_first
                if buffer_saved == 2:
                    policy.exp_replay[1:,...,:3] = state["sliced_buffer"]
                elif buffer_saved == 3:
                    ts = policy.exp_replay[1:,...,:3].shape
                    dt = policy.exp_replay.dtype
                    decomp = decompress(state["compressed_buffer"])
                    policy.exp_replay[1:,...,:3] = np.array(np.frombuffer(decomp, dtype=dt).reshape(ts))
            if buffer_saved > 0:
                policy.retune_selector = state["retune_selector"]
                
            if self.train_exec_impl:
                self.train_exec_impl.shared_metrics.get().restore(
                    state["train_exec_impl"])

    def with_updates(**overrides):
        """Build a copy of this trainer with the specified overrides.

        Arguments:
            overrides (dict): use this to override any of the arguments
                originally passed to build_trainer() for this policy.
        """
        return build_trainer(**dict(original_kwargs, **overrides))

    trainer_cls.with_updates = staticmethod(with_updates)
    trainer_cls.__name__ = name
    trainer_cls.__qualname__ = name
    return trainer_cls
