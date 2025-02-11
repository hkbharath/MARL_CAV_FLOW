import numpy as np
from gym.envs.registration import register
import math
from typing import Tuple

from highway_env import utils
from highway_env.envs.common.abstract import AbstractEnv
from highway_env.envs.common.action import Action
from highway_env.road.road import Road, RoadNetwork
from highway_env.road.lane import AbstractLane
from highway_env.vehicle.dynamics import ControlledBicycleVehicle
from highway_env.vehicle.controller import MDPVehicle
from highway_env.vehicle.kinematics import Vehicle


class LaneChnageMARL(AbstractEnv):

    n_a = 2
    n_s = 25
    
    @classmethod
    def default_config(cls) -> dict:
        config = super().default_config()
        config.update(
            {
                "action": {
                    "type": "MultiAgentAction",
                    "action_config": {
                        "type": "ContinuousAction",
                        "lateral": False,
                        "longitudinal": True,
                        "dynamical": True,
                    },
                },
                "observation": {
                    "type": "MultiAgentObservation",
                    "observation_config": {"type": "Kinematics"},
                },
                "lanes_count": 2,
                "controlled_vehicles": 5,
                "safety_guarantee": False,
                "action_masking": False,
                "target_lane": 0,
                "initial_lane_id": 1,
                "length": 300,
                "screen_width": 1200,
                "screen_height": 100,
                "centering_position": [0.55, 0.5],
                "scaling": 7,
                # "scaling": 4,
                "simulation_frequency": 15,  # [Hz]
                "duration": 20,  # time step
                "policy_frequency": 5,  # [Hz]
                "reward_speed_range": [10, 30],
            }
        )
        return config

    def _reset(self, num_CAV=0) -> None:
        self._create_road()
        self._create_vehicles()
        # self.action_is_safe = True
        self.T = int(self.config["duration"] * self.config["policy_frequency"])

    def _reward(self, action: int) -> float:
        # Cooperative multi-agent reward
        return sum(
            self._agent_reward(action, vehicle) for vehicle in self.controlled_vehicles
        ) / len(self.controlled_vehicles)

    def _agent_reward(self, action: int, vehicle: ControlledBicycleVehicle) -> float:
        """
        The first vehicle is rewarded for 
            - moving towards the middle of target lane,
        All the vehicles are rewarded for
            - moving forward,
            - high speed,
            - headway distance, 
            - avoiding collisions,
            - avoid going off road boundaries.
        :param action: the action performed
        :return: the reward of the state-action transition
        """
        # Optimal reward 0

        last_pos = vehicle.position.copy()
        if len(vehicle.history) > 1:
            last_pos = vehicle.history.popleft()
        
        # reward for moving forward
        dx = vehicle.position[0] - last_pos[0]
        dx_s = utils.lmap(dx, [0, vehicle.LENGTH], [-1, 0])
        
        # cost for moving away from the target lane
        lon, lat = vehicle.target_lane.local_coordinates(vehicle.position)
        lateral_cost = -np.exp(abs(lat)) + 1 # cost is 0 when vehicle is in the middle of the target lane
        # lateral_cost = -dy**2 # Option 2 slighly less cost for lateral position to allow smooth transision
        
        # add cost for not heading towards the direction of the target lane
        heading_err = abs(vehicle.heading*5.09223 + lat) # 5.09223 is multiplication factor to equate penalty for 45 degrees heading to 4m off latral distance from target.
        # according to above equation:
        # heading ~ 0 when vehicle is heading in the direction of motion
        # heading ~ 45 when vehicle is one lane way from target
        # heading ~ 22.5 when vehicle is halfway from target
        # print("heading {}, lateral: {}, heading_err: {}".format(vehicle.heading, lat, heading_err))

        heading_cost = -np.exp(heading_err) + 1 

        # the optimal reward is 1
        speed_s = utils.lmap(
            vehicle.speed, self.config["reward_speed_range"], [0, 1]
        )
        speed_s = np.clip(speed_s, 0, 1)

        # compute headway cost
        headway_distance = self._compute_headway_distance(vehicle)
        headway_cost = (
            np.log(headway_distance / (self.config["HEADWAY_TIME"] * vehicle.speed))
            if vehicle.speed > 0
            else 0
        )
        headway_cost = (headway_cost if headway_cost < 0 else 0)

        # reward for not colliding
        # TODO: test this reward
        # multliy steps/T with 5.311 to equate the cummulative reward of being alive to approximately 2000
        alive_reward = np.exp(self.steps/self.T)

        # compute overall reward
        reward = (
            # self.config["LATERAL_MOTION_COST"] * lateral_cost
            self.config["LATERAL_MOTION_COST"] * heading_cost
            + self.config["HIGH_SPEED_REWARD"] * speed_s
            + self.config["HEADWAY_COST"] * headway_cost
            + self.config["COLLISION_COST"] * (-1 * vehicle.crashed)
            + self.config["ALIVE_REWARD"] * alive_reward
        )
        # print("Stepwise reward: {}".format(reward))
        return reward

    def _create_road(self) -> None:
        """Create a road composed of straight adjacent lanes."""
        self.road = Road(network=RoadNetwork.straight_road_network(self.config["lanes_count"], length= self.config["length"]),
                         np_random=self.np_random, record_history=self.config["show_trajectories"])

    def step(self, action: int) -> Tuple[np.ndarray, float, bool, dict]:
        agent_info = []
        local_info = []

        obs, reward, done, info = super().step(action)
        info["agents_dones"] = tuple(
            self._agent_is_terminal(vehicle) for vehicle in self.controlled_vehicles
        )
        for v in self.controlled_vehicles:
            agent_info.append([v.position[0], v.position[1], v.speed])
            local_coords = v.lane.local_coordinates(v.position)
            local_info.append([local_coords[0], local_coords[1], v.speed])
            if np.isnan(v.position).any():
                raise ValueError("Vehicle position is NaN")
        info["agents_info"] = agent_info
        info["hdv_info"] = local_info

        # hdv_info = []
        # for v in self.road.vehicles:
        #     if v not in self.controlled_vehicles:
        #         hdv_info.append([v.position[0], v.position[1], v.speed])
        # info["hdv_info"] = hdv_info

        for vehicle in self.controlled_vehicles:
            vehicle.local_reward = self._agent_reward(action, vehicle)
        # local reward
        info["agents_rewards"] = tuple(
            vehicle.local_reward for vehicle in self.controlled_vehicles
        )

        obs = np.asarray(obs).reshape((len(obs), -1))
        return obs, reward, done, info
    
    def _cost(self, action: int) -> float:
        """The cost signal is the occurrence of collision."""
        return float(any(vehicle.crashed is False for vehicle in self.controlled_vehicles) )
    
    def _is_terminal(self) -> bool:
        """The episode is over when a collision occurs or when the access ramp has been passed."""
        return (
            any(vehicle.crashed for vehicle in self.controlled_vehicles)
            or self.steps >= self.config["duration"] * self.config["policy_frequency"]
        )

    def _agent_is_terminal(self, vehicle: Vehicle) -> bool:
        """The episode is over when a collision occurs or when the access ramp has been passed."""
        return (
            vehicle.crashed
            or self.steps >= self.config["duration"] * self.config["policy_frequency"]
        )
    
    def _create_vehicles(self) -> None:
        """Create a central vehicle and four other AVs surrounding a main vehicle in random positions."""

        road = self.road
        lane_count = self.config["lanes_count"]
        init_spawn_length = self.config["length"] / 3
        self.controlled_vehicles = []

        lc_spawn_pos = init_spawn_length/2
        # lc_spawn_pos = np.random.choice([-3, -2, -1, 0, 1, 2, 3]) * Vehicle.LENGTH + lc_spawn_pos
        

        # initial speed with noise and location noise
        initial_speed = list (
            np.random.rand(self.config["controlled_vehicles"]) * 2 + 25
        )  # range from [25, 27]


        # Add first vehicle to perform lane change
        target_lane_index = self.config["target_lane"]
        
        # Spwan in lane other than target lane
        # lc_vehicle_spwan_lane = (target_lane_index + np.random.choice([0,1])) % lane_count
        lc_vehicle_spwan_lane = (target_lane_index + 1) % lane_count
        # lc_vehicle_spwan_lane = target_lane_index

        lc_vehicle = self.action_type.vehicle_class(
                road = road,
                position = road.network.get_lane(("0", "1", lc_vehicle_spwan_lane)).position(
                    lc_spawn_pos, 0
                ),
                speed = initial_speed.pop(0),
            )
        lc_vehicle.set_target_lane(target_lane_index)
        self.controlled_vehicles.append(lc_vehicle)
        road.vehicles.append(lc_vehicle)

        # print("LC Vehicle: Spawn lane: {}, Position: {}".format(lc_vehicle_spwan_lane, lc_vehicle.position))

        # Add autonomous vehicles to follow lane
        n_follow_vehicle = self.config["controlled_vehicles"] - 1
        spawn_points = np.random.rand(n_follow_vehicle)
        # CAVs in behind
        spawn_points[:2] = spawn_points[:2] * lc_spawn_pos - 2*Vehicle.LENGTH

        # CAVs front
        spawn_points[2:] = 2*Vehicle.LENGTH + lc_spawn_pos + spawn_points[2:] * (init_spawn_length - lc_spawn_pos)

        spawn_points = list(spawn_points)

        for idx in range(n_follow_vehicle):
            lane_id = idx % lane_count
            lane_follow_vehicle = self.action_type.vehicle_class(
                road = road,
                position = road.network.get_lane(("0", "1", lane_id)).position(
                    spawn_points.pop(0), 0
                ),
                speed = initial_speed.pop(0),
            )
            self.controlled_vehicles.append(lane_follow_vehicle)
            road.vehicles.append(lane_follow_vehicle)
            # print("Other Vehicles: Spawn lane: {}, Position: {}".format(lane_id, lane_follow_vehicle.position))

    def define_spaces(self) -> None:
        """
        Define spaces of agents and observations
        """
        super().define_spaces()
        # enable only first CAV to move laterally
        if len(self.action_type.agents_action_types) > 0:
            self.action_type.agents_action_types[0].lateral = True

    def is_vehicle_on_road(self, vehicle: Vehicle) -> bool:
        if vehicle.position[0] <= 0.0:
            return False
        # center of second lane is 0, so accomodate half of the lane width
        if vehicle.position[1] <= -0.5 * AbstractLane.DEFAULT_WIDTH:
            return False
        # center of second lane is AbstractLane.DEFAULT_WIDTH
        if vehicle.position[1] >= 1.5 * AbstractLane.DEFAULT_WIDTH:
            return False
        # if vehicle.position[0] > self.config["length"]: let the vehicle go out of the max length
        return True
register(
    id="lanechange-marl-v0",
    entry_point="highway_env.envs:LaneChnageMARL",
)

