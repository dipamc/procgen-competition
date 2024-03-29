from ray.rllib.policy.torch_policy import TorchPolicy
import numpy as np
from ray.rllib.utils.torch_ops import convert_to_non_torch_type, convert_to_torch_tensor
from ray.rllib.utils import try_import_torch
from ray.rllib.models import ModelCatalog
from ray.rllib.utils.annotations import override
from collections import deque
from .utils import *
import time

torch, nn = try_import_torch()
from torch.cuda.amp import autocast, GradScaler

class CustomTorchPolicy(TorchPolicy):
    """Example of a random policy
    If you are using tensorflow/pytorch to build custom policies,
    you might find `build_tf_policy` and `build_torch_policy` to
    be useful.
    Adopted from examples from https://docs.ray.io/en/master/rllib-concepts.html
    """

    def __init__(self, observation_space, action_space, config):
        self.config = config
        self.acion_space = action_space
        self.observation_space = observation_space

        self.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        dist_class, logit_dim = ModelCatalog.get_action_dist(
            action_space, self.config["model"], framework="torch")
        self.model = ModelCatalog.get_model_v2(
                        obs_space=observation_space,
                        action_space=action_space,
                        num_outputs=logit_dim,
                        model_config=self.config["model"],
                        framework="torch",
                        device=self.device,
                     )

        TorchPolicy.__init__(
            self,
            observation_space=observation_space,
            action_space=action_space,
            config=config,
            model=self.model,
            loss=None,
            action_distribution_class=dist_class,
        )
        
        self.framework = "torch"

    
    def init_training(self):
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=5e-4)
        self.max_reward = self.config['env_config']['return_max']
        self.rewnorm = RewardNormalizer(cliprew=self.max_reward) ## TODO: Might need to go to custom state
        self.reward_deque = deque(maxlen=100)
        self.best_reward = -np.inf
        self.best_weights = None
        self.time_elapsed = 0
        self.batch_end_time = time.time()
        self.timesteps_total = 0
        self.best_rew_tsteps = 0
        
        nw = self.config['num_workers'] if self.config['num_workers'] > 0 else 1
        self.nbatch = nw * self.config['num_envs_per_worker'] * self.config['rollout_fragment_length']
        self.actual_batch_size = self.nbatch // self.config['updates_per_batch']
        self.accumulate_train_batches = int(np.ceil( self.actual_batch_size / self.config['max_minibatch_size'] ))
        self.mem_limited_batch_size = self.actual_batch_size // self.accumulate_train_batches
        if self.nbatch % self.actual_batch_size != 0 or self.nbatch % self.mem_limited_batch_size != 0:
            print("#################################################")
            print("WARNING: MEMORY LIMITED BATCHING NOT SET PROPERLY")
            print("#################################################")
        self.retune_selector = RetuneSelector(self.nbatch, self.observation_space, self.action_space, 
                                              skips = self.config['retune_skips'], 
                                              replay_size = self.config['retune_replay_size'], 
                                              num_retunes = self.config['num_retunes'])
        self.exp_replay = np.empty((self.retune_selector.replay_size, *self.observation_space.shape), dtype=np.uint8)
        self.target_timesteps = 8_000_000
        self.buffer_time = 20 # TODO: Could try to do a median or mean time step check instead
        self.max_time = self.config['max_time']
        self.maxrewep_lenbuf = deque(maxlen=100)
        self.gamma = self.config['gamma']
        self.adaptive_discount_tuner = AdaptiveDiscountTuner(self.gamma, momentum=0.98, eplenmult=3)
        
        self.lr = self.config['lr']
        self.ent_coef = self.config['entropy_coeff']
        
        self.last_dones = np.zeros((nw * self.config['num_envs_per_worker'],))
        self.save_success = 0
        self.retunes_completed = 0
        self.amp_scaler = GradScaler()
        
    def to_tensor(self, arr):
        return torch.from_numpy(arr).to(self.device)
    
    @override(TorchPolicy)
    def extra_action_out(self, input_dict, state_batches, model, action_dist):
        return {'values': model._value.tolist()}
    
        
    @override(TorchPolicy)
    def learn_on_batch(self, samples):
        """Fused compute gradients and apply gradients call.
        Either this or the combination of compute/apply grads must be
        implemented by subclasses.
        Returns:
            grad_info: dictionary of extra metadata from compute_gradients().
        Examples:
            >>> batch = ev.sample()
            >>> ev.learn_on_batch(samples)
        Reference: https://github.com/ray-project/ray/blob/master/rllib/policy/policy.py#L279-L316
        """
        
        ## Config data values
        nbatch = self.nbatch
        nbatch_train = self.mem_limited_batch_size 
        gamma, lam = self.gamma, self.config['lambda']
        nsteps = self.config['rollout_fragment_length']
        nenvs = nbatch//nsteps
        ts = (nenvs, nsteps)
        mb_dones = unroll(samples['dones'], ts)
        
        ## Reward Normalization - No reward norm works well for many envs
        if self.config['standardize_rewards']:
            mb_origrewards = unroll(samples['rewards'], ts)
            mb_rewards =  np.zeros_like(mb_origrewards)
            mb_rewards[0] = self.rewnorm.normalize(mb_origrewards[0], self.last_dones, 
                                                   self.config["return_reset"])
            for ii in range(1, nsteps):
                mb_rewards[ii] = self.rewnorm.normalize(mb_origrewards[ii], mb_dones[ii-1],
                                                       self.config["return_reset"])
            self.last_dones = mb_dones[-1]
        else:
            mb_rewards = unroll(samples['rewards'], ts)
        
        # Weird hack that helps in many envs (Yes keep it after reward normalization)
        rew_scale = self.config["scale_reward"]
        if rew_scale != 1.0:
            mb_rewards *= rew_scale
        
        should_skip_train_step = self.best_reward_model_select(samples)
        if should_skip_train_step:
            self.update_batch_time()
            return {} # Not doing last optimization step - This is intentional due to noisy gradients
          
        obs = samples['obs']

        ## Value prediction
        next_obs = unroll(samples['new_obs'], ts)[-1]
        last_values, _ = self.model.vf_pi(next_obs, ret_numpy=True, no_grad=True, to_torch=True)
        values = samples['values']
        
        ## GAE
        mb_values = unroll(values, ts)
        mb_returns = np.zeros_like(mb_rewards)
        mb_advs = np.zeros_like(mb_rewards)
        lastgaelam = 0
        for t in reversed(range(nsteps)):
            if t == nsteps - 1:
                nextvalues = last_values
            else:
                nextvalues = mb_values[t+1]
            nextnonterminal = 1.0 - mb_dones[t]
            delta = mb_rewards[t] + gamma * nextvalues * nextnonterminal - mb_values[t]
            mb_advs[t] = lastgaelam = delta + gamma * lam * nextnonterminal * lastgaelam
        mb_returns = mb_advs + mb_values
        
        ## Data from config
        cliprange, vfcliprange = self.config['clip_param'], self.config['vf_clip_param']
        lrnow = self.lr
        max_grad_norm = self.config['grad_clip']
        ent_coef, vf_coef = self.ent_coef, self.config['vf_loss_coeff']
        
        vf_coef_now = vf_coef * self.timesteps_total / self.target_timesteps
        
        neglogpacs = -samples['action_logp'] ## np.isclose seems to be True always, otherwise compute again if needed
        noptepochs = self.config['num_sgd_iter']
        actions = samples['actions']
        returns = roll(mb_returns)
        
        ## Train multiple epochs
        optim_count = 0
        inds = np.arange(nbatch)
        for _ in range(noptepochs):
            np.random.shuffle(inds)
            normalized_advs = returns - values
            # Can do this because actual_batch_size is a multiple of mem_limited_batch_size
            for start in range(0, nbatch, self.actual_batch_size):
                end = start + self.actual_batch_size
                mbinds = inds[start:end]
                advs_batch = normalized_advs[mbinds].copy()
                normalized_advs[mbinds] = (advs_batch - np.mean(advs_batch)) / (np.std(advs_batch) + 1e-8) 
            for start in range(0, nbatch, nbatch_train):
                end = start + nbatch_train
                mbinds = inds[start:end]
                slices = (self.to_tensor(arr[mbinds]) for arr in (obs, returns, actions, values, neglogpacs, normalized_advs))
                optim_count += 1
                apply_grad = (optim_count % self.accumulate_train_batches) == 0
                self._batch_train(apply_grad, self.accumulate_train_batches,
                                  lrnow, cliprange, vfcliprange, max_grad_norm, ent_coef, vf_coef_now, *slices)
        
        ## Distill with augmentation
        should_retune = self.retune_selector.update(obs, self.exp_replay)
        if should_retune:
            self.retune_with_augmentation()
            self.update_batch_time()
            return {}
                
        self.update_gamma(samples)
        self.update_lr()
        self.update_ent_coef()
            
        self.update_batch_time()
        return {}
    
    def update_batch_time(self):
        self.time_elapsed += time.time() - self.batch_end_time
        self.batch_end_time = time.time()
        
    def _batch_train(self, apply_grad, num_accumulate, 
                     lr, cliprange, vfcliprange, max_grad_norm,
                     ent_coef, vf_coef,
                     obs, returns, actions, values, neglogpac_old, advs):
        
        for g in self.optimizer.param_groups:
            g['lr'] = lr
        vpred, pi_logits = self.model.vf_pi(obs, ret_numpy=False, no_grad=False, to_torch=False)
        neglogpac = neglogp_actions(pi_logits, actions)
        entropy = torch.mean(pi_entropy(pi_logits))

        vpredclipped = values + torch.clamp(vpred - values, -vfcliprange, vfcliprange)
        vf_losses1 = torch.pow((vpred - returns), 2)
        vf_losses2 = torch.pow((vpredclipped - returns), 2)
        vf_loss = .5 * torch.mean(torch.max(vf_losses1, vf_losses2))

        ratio = torch.exp(neglogpac_old - neglogpac)
        pg_losses1 = -advs * ratio
        pg_losses2 = -advs * torch.clamp(ratio, 1-cliprange, 1+cliprange)
        pg_loss = torch.mean(torch.max(pg_losses1, pg_losses2))

        loss = pg_loss - entropy * ent_coef + vf_loss * vf_coef
        loss = loss / num_accumulate
        
        # Weird hack to remove vf_loss grad and only keep det_value_loss grad
        if self.model.value_fc.weight.grad is not None:
            self.value_fc_old_grads = [self.model.value_fc.weight.grad.clone(), self.model.value_fc.bias.grad.clone()]
        else:
            self.value_fc_old_grads = None
        loss.backward()
        
        self.model.value_fc.zero_grad() 
        
        latent = self.model._detached_latent
        det_value = self.model.value_fc(latent).squeeze(1)
        det_value_loss = .5 * torch.pow((det_value - returns), 2).mean()
        det_value_loss.backward()
        
        if self.value_fc_old_grads is not None:
            self.model.value_fc.weight.grad += self.value_fc_old_grads[0]
            self.model.value_fc.bias.grad += self.value_fc_old_grads[1]
        
        if apply_grad:
            nn.utils.clip_grad_norm_(self.model.parameters(), max_grad_norm)
            self.optimizer.step()
            self.optimizer.zero_grad()
            
            self.model.value_fc.zero_grad() # Needed for value fc grad hack
            
        
        
    def retune_with_augmentation(self):
        nbatch_train = self.mem_limited_batch_size 
        retune_epochs = self.config['retune_epochs']
        replay_size = self.retune_selector.replay_size
        replay_vf = np.empty((replay_size,), dtype=np.float32)
        replay_pi = np.empty((replay_size, self.retune_selector.ac_space.n), dtype=np.float32)

        # Store current value function and policy logits
        for start in range(0, replay_size, nbatch_train):
            end = start + nbatch_train
            replay_batch = self.exp_replay[start:end]
            replay_vf[start:end], replay_pi[start:end] = self.model.vf_pi(replay_batch, 
                                                                          ret_numpy=True, no_grad=True, to_torch=True)
        
        optim_count = 0
        # Tune vf and pi heads to older predictions with augmented observations
        inds = np.arange(len(self.exp_replay))
        for ep in range(retune_epochs):
            np.random.shuffle(inds)
            for start in range(0, replay_size, nbatch_train):
                end = start + nbatch_train
                mbinds = inds[start:end]
                optim_count += 1
                apply_grad = (optim_count % self.accumulate_train_batches) == 0
                slices = [self.exp_replay[mbinds], 
                          self.to_tensor(replay_vf[mbinds]), 
                          self.to_tensor(replay_pi[mbinds])]
                self.tune_policy(apply_grad, *slices, 0.5)
        
        self.retunes_completed += 1
        self.retune_selector.retune_done()
 
    def tune_policy(self, apply_grad, obs, target_vf, target_pi, retune_vf_loss_coeff):
        obs_aug = np.empty(obs.shape, obs.dtype)
        aug_idx = np.random.randint(3, size=len(obs))
        obs_aug[aug_idx == 0] = pad_and_random_crop(obs[aug_idx == 0], 64, 10)
        obs_aug[aug_idx == 1] = random_cutout_color(obs[aug_idx == 1], 10, 30)
        obs_aug[aug_idx == 2] = obs[aug_idx == 2]
        obs_aug = self.to_tensor(obs_aug)
        with torch.no_grad():
            tpi_log_softmax = nn.functional.log_softmax(target_pi, dim=1)
            tpi_softmax = torch.exp(tpi_log_softmax)
        if not self.config['aux_phase_mixed_precision']:
            loss = self._retune_calc_loss(obs_aug, target_vf, tpi_softmax, tpi_log_softmax, retune_vf_loss_coeff)
            loss.backward()
            if apply_grad:
                self.optimizer.step()
                self.optimizer.zero_grad()
        else:
            with autocast():
                 loss = self._retune_calc_loss(obs_aug, target_vf, tpi_softmax, tpi_log_softmax, retune_vf_loss_coeff)
            self.amp_scaler.scale(loss).backward()
            
            if apply_grad:
                self.amp_scaler.step(self.optimizer)
                self.amp_scaler.update()
                self.optimizer.zero_grad()
            
    def _retune_calc_loss(self, obs_aug, target_vf, tpi_softmax, tpi_log_softmax, retune_vf_loss_coeff):
        vpred, pi_logits = self.model.vf_pi(obs_aug, ret_numpy=False, no_grad=False, to_torch=False)
        pi_log_softmax =  nn.functional.log_softmax(pi_logits, dim=1)
        pi_loss = torch.mean(torch.sum(tpi_softmax * (tpi_log_softmax - pi_log_softmax) , dim=1)) # kl_div torch 1.3.1 has numerical issues
        vf_loss = .5 * torch.mean(torch.pow(vpred - target_vf, 2))
        
        loss = retune_vf_loss_coeff * vf_loss + pi_loss
        loss = loss / self.accumulate_train_batches
        return loss
        
    def best_reward_model_select(self, samples):
        self.timesteps_total += self.nbatch
        
        ## Best reward model selection
        eprews = [info['episode']['r'] for info in samples['infos'] if 'episode' in info]
        self.reward_deque.extend(eprews)
        mean_reward = safe_mean(eprews) if len(eprews) >= 100 else safe_mean(self.reward_deque)
        if self.best_reward < mean_reward:
            self.best_reward = mean_reward
            self.best_weights = self.get_weights()["current_weights"]
            self.best_rew_tsteps = self.timesteps_total
           
        if self.timesteps_total > self.target_timesteps or (self.time_elapsed + self.buffer_time) > self.max_time:
            if self.best_weights is not None:
                self.set_model_weights(self.best_weights)
                return True
            
        return False
    
    def update_lr(self):
        if self.config['lr_schedule'] == 'linear':
            self.lr = linear_schedule(initial_val=self.config['lr'],
                                      final_val=self.config['final_lr'],
                                      current_steps=self.timesteps_total,
                                      total_steps=self.target_timesteps)
            
        elif self.config['lr_schedule'] == 'exponential':
            self.lr = 0.997 * self.lr 

    
    def update_ent_coef(self):
        if self.config['entropy_schedule']:
            self.ent_coef = linear_schedule(initial_val=self.config['entropy_coeff'], 
                                            final_val=self.config['final_entropy_coeff'], 
                                            current_steps=self.timesteps_total, 
                                            total_steps=self.target_timesteps)
    
    def update_gamma(self, samples):
        if self.config['adaptive_gamma']:
            epinfobuf = [info['episode'] for info in samples['infos'] if 'episode' in info]
            self.maxrewep_lenbuf.extend([epinfo['l'] for epinfo in epinfobuf if epinfo['r'] >= self.max_reward])
            sorted_nth = lambda buf, n: np.nan if len(buf) < 100 else sorted(self.maxrewep_lenbuf.copy())[n]
            target_horizon = sorted_nth(self.maxrewep_lenbuf, 80)
            self.gamma = self.adaptive_discount_tuner.update(target_horizon)

        
    def get_custom_state_vars(self):
        return {
            "time_elapsed": self.time_elapsed,
            "timesteps_total": self.timesteps_total,
            "best_weights": self.best_weights,
            "reward_deque": self.reward_deque,
            "batch_end_time": self.batch_end_time,
            "gamma": self.gamma,
            "maxrewep_lenbuf": self.maxrewep_lenbuf,
            "num_retunes": self.retune_selector.num_retunes,
            "lr": self.lr,
            "ent_coef": self.ent_coef,
            "rewnorm": self.rewnorm,
            "best_rew_tsteps": self.best_rew_tsteps,
            "best_reward": self.best_reward,
            "last_dones": self.last_dones,
            "retunes_completed": self.retunes_completed,
        }
    
    def set_custom_state_vars(self, custom_state_vars):
        self.time_elapsed = custom_state_vars["time_elapsed"]
        self.timesteps_total = custom_state_vars["timesteps_total"]
        self.best_weights = custom_state_vars["best_weights"]
        self.reward_deque = custom_state_vars["reward_deque"]
        self.batch_end_time = custom_state_vars["batch_end_time"]
        self.gamma = self.adaptive_discount_tuner.gamma = custom_state_vars["gamma"]
        self.retune_selector.set_num_retunes(custom_state_vars["num_retunes"])
        self.maxrewep_lenbuf = custom_state_vars["maxrewep_lenbuf"]
        self.lr =custom_state_vars["lr"]
        self.ent_coef = custom_state_vars["ent_coef"]
        self.rewnorm = custom_state_vars["rewnorm"]
        self.best_rew_tsteps = custom_state_vars["best_rew_tsteps"]
        self.best_reward = custom_state_vars["best_reward"]
        self.last_dones = custom_state_vars["last_dones"]
        self.retunes_completed = custom_state_vars["retunes_completed"]
    
    @override(TorchPolicy)
    def get_weights(self):
        weights = {}
        weights["current_weights"] = {
            k: v.cpu().detach().numpy()
            for k, v in self.model.state_dict().items()
        }
        return weights
        
    
    @override(TorchPolicy)
    def set_weights(self, weights):
        self.set_model_weights(weights["current_weights"])
        
    def set_optimizer_state(self, optimizer_state, amp_scaler_state):
        optimizer_state = convert_to_torch_tensor(optimizer_state, device=self.device)
        self.optimizer.load_state_dict(optimizer_state)
        
        amp_scaler_state = convert_to_torch_tensor(amp_scaler_state, device=self.device)
        self.amp_scaler.load_state_dict(amp_scaler_state)
        
    def set_model_weights(self, model_weights):
        model_weights = convert_to_torch_tensor(model_weights, device=self.device)
        self.model.load_state_dict(model_weights)