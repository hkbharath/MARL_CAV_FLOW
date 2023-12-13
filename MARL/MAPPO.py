import torch as th
from torch import nn
import configparser

config_dir = 'configs/configs_ppo.ini'
config = configparser.ConfigParser()
config.read(config_dir)
torch_seed = config.getint('MODEL_CONFIG', 'torch_seed')
th.manual_seed(torch_seed)
th.backends.cudnn.benchmark = False
th.backends.cudnn.deterministic = True

from torch.optim import Adam, RMSprop

import numpy as np
import os, logging
from copy import deepcopy
from single_agent.Memory_common import OnPolicyReplayMemory
from single_agent.Model_common import ActorNetwork, CriticNetwork
from common.utils import index_to_one_hot, to_tensor_var, VideoRecorder
import matplotlib.pyplot as plt

class MAPPO:
    """
    An multi-agent learned with PPO
    reference: https://github.com/ChenglongChen/pytorch-DRL
    """
    def __init__(self, env, state_dim, action_dim,
                 memory_capacity=10000, max_steps=None,
                 roll_out_n_steps=1, target_tau=1.,
                 target_update_steps=5, clip_param=0.2,
                 reward_gamma=0.99, reward_scale=20,
                 actor_hidden_size=128, critic_hidden_size=128,
                 actor_output_act=th.sigmoid, critic_loss="mse",
                 actor_lr=0.0001, critic_lr=0.0001, test_seeds=0,
                 optimizer_type="rmsprop", entropy_reg=0.01,
                 max_grad_norm=0.5, batch_size=100, episodes_before_train=100,
                 use_cuda=True, traffic_density=1, reward_type="global_R", render = False):

        assert traffic_density in [1, 2, 3]
        assert reward_type in ["regional_R", "global_R"]
        self.reward_type = reward_type
        self.env = env
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.env_state, self.action_mask = self.env.reset()
        self.n_episodes = 0
        self.n_steps = 0
        self.max_steps = max_steps
        self.test_seeds = test_seeds
        self.reward_gamma = reward_gamma
        self.reward_scale = reward_scale
        self.traffic_density = traffic_density
        self.memory = OnPolicyReplayMemory(memory_capacity)
        self.actor_hidden_size = actor_hidden_size
        self.critic_hidden_size = critic_hidden_size
        self.actor_output_act = actor_output_act
        self.critic_loss = critic_loss
        self.actor_lr = actor_lr
        self.critic_lr = critic_lr
        self.optimizer_type = optimizer_type
        self.entropy_reg = entropy_reg
        self.max_grad_norm = max_grad_norm
        self.batch_size = batch_size
        self.episodes_before_train = episodes_before_train
        self.use_cuda = use_cuda and th.cuda.is_available()
        self.roll_out_n_steps = roll_out_n_steps
        self.target_tau = target_tau
        self.target_update_steps = target_update_steps
        self.clip_param = clip_param
        self.render = render

        self.actor = ActorNetwork(self.state_dim, self.actor_hidden_size,
                                  self.action_dim, self.actor_output_act)
        self.critic = CriticNetwork(self.state_dim, self.action_dim, self.critic_hidden_size, 1)

        # to ensure target network and learning network has the same weights
        self.actor_target = deepcopy(self.actor)
        self.critic_target = deepcopy(self.critic)

        if self.optimizer_type == "adam":
            self.actor_optimizer = Adam(self.actor.parameters(), lr=self.actor_lr)
            self.critic_optimizer = Adam(self.critic.parameters(), lr=self.critic_lr)
        elif self.optimizer_type == "rmsprop":
            self.actor_optimizer = RMSprop(self.actor.parameters(), lr=self.actor_lr)
            self.critic_optimizer = RMSprop(self.critic.parameters(), lr=self.critic_lr)

        if self.use_cuda:
            self.actor.cuda()
            self.critic.cuda()
            self.actor_target.cuda()
            self.critic_target.cuda()

        self.episode_rewards = [0]
        self.average_speed = [0]
        self.epoch_steps = [0]

        self.cav_pos = []
        self.hdv_pos = []

    # agent interact with the environment to collect experience
    def interact(self):
        if (self.max_steps is not None) and (self.n_steps >= self.max_steps):
            self.env_state, _ = self.env.reset()
            self.n_steps = 0
        states = []
        actions = []
        rewards = []
        done = True
        average_speed = 0

        self.n_agents = len(self.env.controlled_vehicles)
        # take n steps
        for i in range(self.roll_out_n_steps):
            states.append(self.env_state)
            action = self.exploration_action(self.env_state, self.n_agents)
            # for idx in range(len(action)):
            #     action[idx] = np.zeros(action[idx].shape)
            # print(action)    
            next_state, global_reward, done, info = self.env.step(tuple(action))
            actions.append(action)
            self.episode_rewards[-1] += global_reward
            self.epoch_steps[-1] += 1
            if self.reward_type == "regional_R":
                reward = info["regional_rewards"]
            elif self.reward_type == "global_R":
                reward = [global_reward] * self.n_agents
            rewards.append(reward)
            average_speed += info["average_speed"]
            final_state = next_state
            self.env_state = next_state

            self.n_steps += 1

            self.cav_pos.extend(info["agents_info"])
            self.hdv_pos.extend(info["hdv_info"])
            if done:
                self.env_state, _ = self.env.reset()
                break

        # discount reward
        if done:
            final_value = [0.0] * self.n_agents
            self.n_episodes += 1
            self.episode_done = True
            self.episode_rewards.append(0)
            self.average_speed[-1] = average_speed / self.epoch_steps[-1]
            self.average_speed.append(0)
            self.epoch_steps.append(0)
            # self.debug_vehicle_position()
            self.cav_pos = []
            self.hdv_pos = []
        else:
            self.episode_done = False
            final_action = self.action(final_state, self.n_agents)
            final_value = self.value(final_state, final_action)

        if self.reward_scale > 0:
            rewards = np.array(rewards) / self.reward_scale

        for agent_id in range(self.n_agents):
            rewards[:, agent_id] = self._discount_reward(rewards[:, agent_id], final_value[agent_id])

        rewards = rewards.tolist()
        self.memory.push(states, actions, rewards)

    # train on a roll out batch
    def train(self):
        if self.n_episodes <= self.episodes_before_train:
            pass

        batch = self.memory.sample(self.batch_size)
        states_var = to_tensor_var(batch.states, self.use_cuda).view(-1, self.n_agents, self.state_dim)
        actions_var = to_tensor_var(batch.actions, self.use_cuda).view(-1, self.n_agents, self.action_dim)
        rewards_var = to_tensor_var(batch.rewards, self.use_cuda).view(-1, self.n_agents, 1)

        for agent_id in range(self.n_agents):
            # update actor network
            self.actor_optimizer.zero_grad()
            values = self.critic_target(states_var[:, agent_id, :], actions_var[:, agent_id, :]).detach()
            advantages = rewards_var[:, agent_id, :] - values

            action_log_probs = self.actor(states_var[:, agent_id, :])
            action_log_probs = th.sum(action_log_probs * actions_var[:, agent_id, :], 1)
            old_action_log_probs = self.actor_target(states_var[:, agent_id, :]).detach()
            old_action_log_probs = th.sum(old_action_log_probs * actions_var[:, agent_id, :], 1)
            ratio = th.exp(action_log_probs - old_action_log_probs)
            surr1 = ratio * advantages
            surr2 = th.clamp(ratio, 1.0 - self.clip_param, 1.0 + self.clip_param) * advantages
            # PPO's pessimistic surrogate (L^CLIP)
            actor_loss = -th.mean(th.min(surr1, surr2))
            actor_loss.backward()
            if self.max_grad_norm is not None:
                nn.utils.clip_grad_norm_(self.actor.parameters(), self.max_grad_norm)
            self.actor_optimizer.step()

            # update critic network
            self.critic_optimizer.zero_grad()
            target_values = rewards_var[:, agent_id, :]
            values = self.critic(states_var[:, agent_id, :], actions_var[:, agent_id, :])
            if self.critic_loss == "huber":
                critic_loss = nn.functional.smooth_l1_loss(values, target_values)
            else:
                critic_loss = nn.MSELoss()(values, target_values)
            critic_loss.backward()
            if self.max_grad_norm is not None:
                nn.utils.clip_grad_norm_(self.critic.parameters(), self.max_grad_norm)
            self.critic_optimizer.step()

        # update actor target network and critic target network
        if self.n_episodes % self.target_update_steps == 0 and self.n_episodes > 0:
            self._soft_update_target(self.actor_target, self.actor)
            self._soft_update_target(self.critic_target, self.critic)

    # predict softmax action based on state
    def _continuous_action(self, state, n_agents):
        state_var = to_tensor_var([state], self.use_cuda)
        # print(state_var[:, 0, :])

        continuous_action = []
        for agent_id in range(n_agents):
            # continuous_action_var = th.exp(self.actor(state_var[:, agent_id, :]))
            continuous_action_var = self.actor(state_var[:, agent_id, :])
            
            if self.use_cuda:
                continuous_action.append(continuous_action_var.data.cpu().numpy()[0])
            else:
                continuous_action.append(continuous_action_var.data.numpy()[0])
        return continuous_action

    # choose an action based on state with random noise added for exploration in training
    def exploration_action(self, state, n_agents):
        continuous_actions = self._continuous_action(state, n_agents)
        actions = []
        for pi in continuous_actions:
            noise = 0.5 * np.random.randn(*pi.shape)
            actions.append(pi + noise)
        return actions

    # choose an action based on state for execution
    def action(self, state, n_agents):
        continuous_actions = self._continuous_action(state, n_agents)
        actions = []
        # very mild noise for variability
        for pi in continuous_actions:
            noise = 0.01 * np.random.randn(*pi.shape)
            actions.append(pi + noise) # Add noise to test
        return actions

    # evaluate value for a state-action pair
    def value(self, state, action):
        state_var = to_tensor_var([state], self.use_cuda)
        # action = index_to_one_hot(action, self.action_dim)
        action_var = to_tensor_var([action], self.use_cuda)

        values = [0] * self.n_agents
        for agent_id in range(self.n_agents):
            value_var = self.critic(state_var[:, agent_id, :], action_var[:, agent_id, :])

            if self.use_cuda:
                values[agent_id] = value_var.data.cpu().numpy()[0]
            else:
                values[agent_id] = value_var.data.numpy()[0]
        return values

    # evaluation the learned agent
    def evaluation(self, env, output_dir, eval_episodes=1, is_train=True):
        rewards = []
        infos = []
        avg_speeds = []
        steps = []
        vehicle_speed = []
        vehicle_position = []
        video_recorder = None
        seeds = [int(s) for s in self.test_seeds.split(',')]
        wait_once = True

        for i in range(eval_episodes):
            avg_speed = 0
            step = 0
            rewards_i = []
            infos_i = []
            self.cav_pos = []
            self.hdv_pos = []
            done = False
            if is_train:
                if self.traffic_density == 1:
                    state, action_mask = env.reset(is_training=False, testing_seeds=seeds[i], num_CAV=i + 1)
                elif self.traffic_density == 2:
                    state, action_mask = env.reset(is_training=False, testing_seeds=seeds[i], num_CAV=i + 2)
                elif self.traffic_density == 3:
                    state, action_mask = env.reset(is_training=False, testing_seeds=seeds[i], num_CAV=i + 4)
            else:
                state, action_mask = env.reset(is_training=False, testing_seeds=seeds[i])

            n_agents = len(env.controlled_vehicles)

            if self.render:
                rendered_frame = env.render(mode="rgb_array")
                video_filename = os.path.join(output_dir,
                                            "testing_episode{}".format(self.n_episodes + 1) + '_{}'.format(i) +
                                            '.mp4')
                # Init video recording
                if video_filename is not None:
                    print("Recording video to {} ({}x{}x{}@{}fps)".format(video_filename, *rendered_frame.shape,
                                                                          5))
                    video_recorder = VideoRecorder(video_filename,
                                                   frame_size=rendered_frame.shape, fps=5)
                    video_recorder.add_frame(rendered_frame)
                else:
                    video_recorder = None
            actions = []
            while not done:
                step += 1
                action = self.action(state, n_agents)
                # for idx in range(len(actions)):
                #     actions[idx] = np.zeros(actions[idx].shape)
                # print(action)
                actions.append(action)
                state, reward, done, info = env.step(action)
                # print(reward)
                # print(info)
                avg_speed += info["average_speed"]
                if self.render:
                    rendered_frame = env.render(mode="rgb_array")
                    if video_recorder is not None:
                        video_recorder.add_frame(rendered_frame)
                if step == 3 and wait_once:
                    input("Press Enter to continue...")
                    wait_once = False

                rewards_i.append(reward)
                infos_i.append(info)
                self.cav_pos.extend(info["agents_info"])
                self.hdv_pos.extend(info["hdv_info"])

            vehicle_speed.append(info["vehicle_speed"])
            vehicle_position.append(info["vehicle_position"])
            rewards.append(rewards_i)
            infos.append(infos_i)
            steps.append(step)
            avg_speeds.append(avg_speed / step)
            # self.debug_vehicle_position()
            # create_action_distribution(np.array(actions).reshape(-1, 2))
            # create_line_plot(np.array(rewards_i).reshape(-1, 1))
            
            lc_veh_action = np.array(actions)[:,0]
            print("Acceleration", lc_veh_action[:,0])
            print("Steering", lc_veh_action[:,1])
            create_line_plot(lc_veh_action[:,0], title="Actions", ylabel="Acceleration", xlabel="Step")
            create_line_plot(lc_veh_action[:,1], title="Actions", ylabel="Steering", xlabel="Step")

        if video_recorder is not None:
            video_recorder.release()
        env.close()
        return rewards, (vehicle_speed, vehicle_position), steps, avg_speeds

    # discount roll out rewards
    def _discount_reward(self, rewards, final_value):
        discounted_r = np.zeros_like(rewards)
        running_add = final_value
        for t in reversed(range(0, len(rewards))):
            running_add = running_add * self.reward_gamma + rewards[t]
            discounted_r[t] = running_add
        return discounted_r

    # soft update the actor target network or critic target network
    def _soft_update_target(self, target, source):
        for t, s in zip(target.parameters(), source.parameters()):
            t.data.copy_(
                (1. - self.target_tau) * t.data + self.target_tau * s.data)

    def load(self, model_dir, global_step=None, train_mode=False):
        save_file = None
        save_step = 0
        if os.path.exists(model_dir):
            if global_step is None:
                for file in os.listdir(model_dir):
                    if file.startswith('checkpoint'):
                        tokens = file.split('.')[0].split('-')
                        if len(tokens) != 2:
                            continue
                        cur_step = int(tokens[1])
                        if cur_step > save_step:
                            save_file = file
                            save_step = cur_step
            else:
                save_file = 'checkpoint-{:d}.pt'.format(global_step)
        if save_file is not None:
            file_path = model_dir + save_file
            checkpoint = th.load(file_path, 
                                 map_location=th.device('cuda' if self.use_cuda else 'cpu'))
            print('Checkpoint loaded: {}'.format(file_path))
            self.actor.load_state_dict(checkpoint['model_state_dict'])
            if train_mode:
                self.actor_optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
                self.actor.train()
            else:
                self.actor.eval()
            return True
        logging.error('Can not find checkpoint for {}'.format(model_dir))
        return False

    def save(self, model_dir, global_step):
        file_path = model_dir + 'checkpoint-{:d}.pt'.format(global_step)
        th.save({'global_step': global_step,
                 'model_state_dict': self.actor.state_dict(),
                 'optimizer_state_dict': self.actor_optimizer.state_dict()},
                file_path)
    
    def debug_vehicle_position(self):
        cav = np.array(self.cav_pos)
        hdv = np.array(self.hdv_pos)
        
        plt.figure(figsize=(12,5))
        
        # plt.subplot(1,2,1)
        create_scatter_plot(cav, hdv)

        # plt.subplot(1,2,2)
        # create_line_plot(cav, hdv)     

        plt.tight_layout() 
        plt.show()

def create_action_distribution(actions):
    plt.title("Actions")
    # plt.hist(actions[:0], bins=100, range=(0, 100), color='red', label='acceleration', alpha=0.5)
    plt.hist(actions, bins=100, range=(0, 1), color=["red", "blue"], label=['acceleration','steering'], alpha=0.5)
    plt.xlabel('Action')
    plt.ylabel('Frequency')
    plt.legend()
    plt.show()

def create_scatter_plot(cav, hdv, title="Positions"):
    plt.title(title)
    # Create a scatter plot for the cav
    plt.scatter(cav[:, 0], cav[:, 1], label='CAV', color='red', alpha=0.5)

    # Create a scatter plot for the hdv
    # plt.scatter(hdv[:, 0], hdv[:, 1], label='HDV', color='blue', alpha=0.5)

    # Add labels and a legend
    plt.xlabel('X-pos')
    plt.ylabel('Y-pos')
    plt.legend()

# def create_line_plot(cav, hdv, title = "Speed"):
#     x = range(len(cav))
#     plt.plot(x, cav[:, 2], label='CAV', color='red', linestyle='-')
#     plt.plot(x, hdv[:, 2], label='HDV', color='blue', linestyle='-')
#     plt.title(title)
#     plt.xlabel("Espisode")
#     plt.ylabel("Speed")
#     plt.legend()

def create_line_plot(s, title = "Speed", ylabel = "Reward", xlabel = "Episode"):
    x = range(len(s))
    plt.plot(x, s, color='red', linestyle='-')
    plt.title(title)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.legend()
    plt.show()