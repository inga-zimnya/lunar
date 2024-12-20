__credits__ = ["Andrea PIERRÉ"]

import math
from typing import TYPE_CHECKING, Optional
import pygame

from PIL import Image
import numpy as np
import json

import gymnasium as gym
from gymnasium import error, spaces
from gymnasium.error import DependencyNotInstalled
from gymnasium.utils import EzPickle
from gymnasium.utils.step_api_compatibility import step_api_compatibility


try:
    import Box2D
    from Box2D.b2 import (
        circleShape,
        contactListener,
        edgeShape,
        fixtureDef,
        polygonShape,
        revoluteJointDef,
    )
except ImportError as e:
    raise DependencyNotInstalled(
        'Box2D is not installed, you can install it by run `pip install swig` followed by `pip install "gymnasium[box2d]"`'
    ) from e


if TYPE_CHECKING:
    import pygame

with open('input.json', 'r') as file:
    config = json.load(file)

FPS = config["constants"]["FPS"]
SCALE = config["constants"]["SCALE"]

MAIN_ENGINE_POWER = config["constants"]["MAIN_ENGINE_POWER"]
SIDE_ENGINE_POWER = config["constants"]["SIDE_ENGINE_POWER"]

INITIAL_RANDOM = config["constants"]["INITIAL_RANDOM"]  # Set 1500 to make game harder

LANDER_POLY = [(-14, +17), (-17, 0), (-17, -10), (+17, -10), (+17, 0), (+14, +17)]
LEG_AWAY = config["constants"]["LEG_AWAY"]
LEG_DOWN = config["constants"]["LEG_DOWN"]
LEG_W = config["constants"]["LEG_W"]
LEG_H = config["constants"]["LEG_H"]
LEG_SPRING_TORQUE = config["constants"]["LEG_SPRING_TORQUE"]

SIDE_ENGINE_HEIGHT = config["constants"]["SIDE_ENGINE_HEIGHT"]
SIDE_ENGINE_AWAY = config["constants"]["SIDE_ENGINE_AWAY"]
MAIN_ENGINE_Y_LOCATION = config["constants"]["MAIN_ENGINE_Y_LOCATION"]

VIEWPORT_W = config["constants"]["VIEWPORT_W"]
VIEWPORT_H = config["constants"]["VIEWPORT_H"]

class ContactDetector(contactListener):
    def __init__(self, env):
        contactListener.__init__(self)
        self.env = env

    def BeginContact(self, contact):
        if (
            self.env.lander == contact.fixtureA.body
            or self.env.lander == contact.fixtureB.body
        ):
            self.env.game_over = True
        for i in range(2):
            if self.env.legs[i] in [contact.fixtureA.body, contact.fixtureB.body]:
                self.env.legs[i].ground_contact = True

    def EndContact(self, contact):
        for i in range(2):
            if self.env.legs[i] in [contact.fixtureA.body, contact.fixtureB.body]:
                self.env.legs[i].ground_contact = False


class LunarLander(gym.Env, EzPickle):

    metadata = {
        "render_modes": ["human", "rgb_array"],
        "render_fps": FPS,
    }

    def __init__(
        self,
            config_path: str = "input.json",
            render_mode: Optional[str] = None,
            continuous: Optional[bool] = None,
            gravity: Optional[float] = None,
            enable_wind: Optional[bool] = None,
            wind_power: Optional[float] = None,
            turbulence_power: Optional[float] = None,
            experiment_number: int = 1
    ):
        EzPickle.__init__(
            self,
            render_mode,
            continuous,
            gravity,
            enable_wind,
            wind_power,
            turbulence_power,
        )

        # Load configuration from JSON
        with open(config_path, "r") as f:
            config = json.load(f)

        constants = config["constants"]
        main_config = config["main"]
        initial_position_and_velocity_config = config["initial_position_and_velocity"]
        obs_space_config = config["observation_space"]
        reward_function_config = config["reward_function"]

        # Initialize variables from JSON or use provided parameters
        self.experiment_number = experiment_number
        self.render_mode = render_mode or main_config["render_mode"]
        self.continuous = continuous if continuous is not None else main_config["continuous"]
        self.gravity = gravity if gravity is not None else main_config["gravity"]
        self.enable_wind = enable_wind if enable_wind is not None else main_config["enable_wind"]
        self.wind_power = wind_power if wind_power is not None else main_config["wind_power"]
        self.turbulence_power = turbulence_power if turbulence_power is not None else main_config["turbulence_power"]

        self.density = initial_position_and_velocity_config['density']
        self.friction = initial_position_and_velocity_config['friction']
        self.restitution = initial_position_and_velocity_config['restitution']

        self.low_x = obs_space_config['low_x']
        self.low_y = obs_space_config['low_y']
        self.high_x = obs_space_config['high_x']
        self.high_y = obs_space_config['high_y']

        self.shaping_0 = reward_function_config['shaping_0']
        self.shaping_1 = reward_function_config['shaping_1']
        self.shaping_2 = reward_function_config['shaping_2']
        self.shaping_3 = reward_function_config['shaping_3']
        self.shaping_4 = reward_function_config['shaping_4']
        self.m_power_coef = reward_function_config['m_power_coef']

        # Metadata setup
        self.metadata["render_fps"] = constants["FPS"]

        gravity = config["main"].get("gravity", -10.0)

        assert (
                -12.0 < gravity < 0.0
        ), f"gravity (current value: {gravity}) must be between -12 and 0"
        self.gravity = gravity

        # print(wind_power, main_config["wind_power"])

        if 0.0 > self.wind_power or self.wind_power > 20.0:
            gym.logger.warn(
                f"wind_power value is recommended to be between 0.0 and 20.0, (current value: {wind_power})"
            )

        if 0.0 > self.turbulence_power or self.turbulence_power > 2.0:
            gym.logger.warn(
                f"turbulence_power value is recommended to be between 0.0 and 2.0, (current value: {turbulence_power})"
            )

        self.isopen = True
        self.world = Box2D.b2World(gravity=(0, self.gravity))

        self.screen: pygame.Surface = None
        self.clock = None

        self.moon = None
        self.lander: Optional[Box2D.b2Body] = None
        self.particles = []

        self.prev_reward = None

        low = np.array(
            [
                # these are bounds for position
                # realistically the environment should have ended
                # long before we reach more than 50% outside
                self.low_x,
                self.low_y,
                # velocity bounds is 5x rated speed
                -10.0,
                -10.0,
                -2 * math.pi,
                -10.0,
                -0.0,
                -0.0,
            ]
        ).astype(np.float32)
        high = np.array(
            [
                # these are bounds for position
                # realistically the environment should have ended
                # long before we reach more than 50% outside
                self.high_x,
                self.high_y,
                # velocity bounds is 5x rated speed
                10.0,
                10.0,
                2 * math.pi,
                10.0,
                1.0,
                1.0,
            ]
        ).astype(np.float32)

        # useful range is -1 .. +1, but spikes can be higher
        self.observation_space = spaces.Box(low, high)

        if self.continuous:
            # Action is two floats [main engine, left-right engines].
            # Main engine: -1..0 off, 0..+1 throttle from 50% to 100% power. Engine can't work with less than 50% power.
            # Left-right:  -1.0..-0.5 fire left engine, +0.5..+1.0 fire right engine, -0.5..0.5 off
            self.action_space = spaces.Box(-1, +1, (2,), dtype=np.float32)
        else:
            # Nop, fire left engine, main engine, right engine
            self.action_space = spaces.Discrete(4)

        # Simulation state tracking
        self.results = {
            "experiment_number": self.experiment_number,
            "input_parameters": {
                "gravity": self.gravity,
                "wind_power": self.wind_power,
                "turbulence_power": self.turbulence_power,
                "density": self.density,
                "friction": self.friction,
                "restitution": self.restitution,
                "low_x": self.low_x,
                "low_y": self.low_y,
                "high_x": self.high_x,
                "high_y": self.high_y,
                "shaping_0": self.shaping_0,
                "shaping_1": self.shaping_1,
                "shaping_2": self.shaping_2,
                "shaping_3": self.shaping_3,
                "shaping_4": self.shaping_4,
                "m_power_coef": self.m_power_coef
            },
            "intermediate_states": [],
        }

        print("results", self.results)

        # Rendering and frame capture
        self.frames = []


    def _destroy(self):
        if not self.moon:
            return
        self.world.contactListener = None
        self._clean_particles(True)
        self.world.DestroyBody(self.moon)
        self.moon = None
        self.world.DestroyBody(self.lander)
        self.lander = None
        self.world.DestroyBody(self.legs[0])
        self.world.DestroyBody(self.legs[1])

    def reset(
        self,
        *,
        seed: Optional[int] = None,
        options: Optional[dict] = None,
    ):
        super().reset(seed=seed)
        self._destroy()

        # Bug's workaround for: https://github.com/Farama-Foundation/Gymnasium/issues/728
        # Not sure why the self._destroy() is not enough to clean(reset) the total world environment elements, need more investigation on the root cause,
        # we must create a totally new world for self.reset(), or the bug#728 will happen
        self.world = Box2D.b2World(gravity=(0, self.gravity))
        self.world.contactListener_keepref = ContactDetector(self)
        self.world.contactListener = self.world.contactListener_keepref
        self.game_over = False
        self.prev_shaping = None

        W = VIEWPORT_W / SCALE
        H = VIEWPORT_H / SCALE

        # Create Terrain
        CHUNKS = 11
        height = self.np_random.uniform(0, H / 2, size=(CHUNKS + 1,))
        chunk_x = [W / (CHUNKS - 1) * i for i in range(CHUNKS)]
        self.helipad_x1 = chunk_x[CHUNKS // 2 - 1]
        self.helipad_x2 = chunk_x[CHUNKS // 2 + 1]
        self.helipad_y = H / 4
        height[CHUNKS // 2 - 2] = self.helipad_y
        height[CHUNKS // 2 - 1] = self.helipad_y
        height[CHUNKS // 2 + 0] = self.helipad_y
        height[CHUNKS // 2 + 1] = self.helipad_y
        height[CHUNKS // 2 + 2] = self.helipad_y
        smooth_y = [
            0.33 * (height[i - 1] + height[i + 0] + height[i + 1])
            for i in range(CHUNKS)
        ]

        self.moon = self.world.CreateStaticBody(
            shapes=edgeShape(vertices=[(0, 0), (W, 0)])
        )
        self.sky_polys = []
        for i in range(CHUNKS - 1):
            p1 = (chunk_x[i], smooth_y[i])
            p2 = (chunk_x[i + 1], smooth_y[i + 1])
            self.moon.CreateEdgeFixture(vertices=[p1, p2], density=0, friction=0.1)
            self.sky_polys.append([p1, p2, (p2[0], H), (p1[0], H)])

        self.moon.color1 = (0.0, 0.0, 0.0)
        self.moon.color2 = (0.0, 0.0, 0.0)

        # Create Lander body
        initial_y = VIEWPORT_H / SCALE
        initial_x = VIEWPORT_W / SCALE / 2
        self.lander = self.world.CreateDynamicBody(
            position=(initial_x, initial_y),
            angle=0.0,
            fixtures=fixtureDef(
                shape=polygonShape(
                    vertices=[(x / SCALE, y / SCALE) for x, y in LANDER_POLY]
                ),
                density=self.density,
                friction=self.friction,
                categoryBits=0x0010,
                maskBits=0x001,  # collide only with ground
                restitution=self.restitution,
            ),  # 0.99 bouncy
        )
        self.lander.color1 = (128, 102, 230)
        self.lander.color2 = (77, 77, 128)

        # Apply the initial random impulse to the lander
        self.lander.ApplyForceToCenter(
            (
                self.np_random.uniform(-INITIAL_RANDOM, INITIAL_RANDOM),
                self.np_random.uniform(-INITIAL_RANDOM, INITIAL_RANDOM),
            ),
            True,
        )

        if self.enable_wind:  # Initialize wind pattern based on index
            self.wind_idx = self.np_random.integers(-9999, 9999)
            self.torque_idx = self.np_random.integers(-9999, 9999)

        # Create Lander Legs
        self.legs = []
        for i in [-1, +1]:
            leg = self.world.CreateDynamicBody(
                position=(initial_x - i * LEG_AWAY / SCALE, initial_y),
                angle=(i * 0.05),
                fixtures=fixtureDef(
                    shape=polygonShape(box=(LEG_W / SCALE, LEG_H / SCALE)),
                    density=1.0,
                    restitution=0.0,
                    categoryBits=0x0020,
                    maskBits=0x001,
                ),
            )
            leg.ground_contact = False
            leg.color1 = (128, 102, 230)
            leg.color2 = (77, 77, 128)
            rjd = revoluteJointDef(
                bodyA=self.lander,
                bodyB=leg,
                localAnchorA=(0, 0),
                localAnchorB=(i * LEG_AWAY / SCALE, LEG_DOWN / SCALE),
                enableMotor=True,
                enableLimit=True,
                maxMotorTorque=LEG_SPRING_TORQUE,
                motorSpeed=+0.3 * i,  # low enough not to jump back into the sky
            )
            if i == -1:
                rjd.lowerAngle = (
                    +0.9 - 0.5
                )  # The most esoteric numbers here, angled legs have freedom to travel within
                rjd.upperAngle = +0.9
            else:
                rjd.lowerAngle = -0.9
                rjd.upperAngle = -0.9 + 0.5
            leg.joint = self.world.CreateJoint(rjd)
            self.legs.append(leg)

        self.drawlist = [self.lander] + self.legs

        self.experiment_number += 1
        # self.results = []
        # self.frames = []

        if self.render_mode == "human":
            self.render()
        return self.step(np.array([0, 0]) if self.continuous else 0)[0], {}

        # self.frames.clear()
        self.results["intermediate_states"].clear()
        print('clear', self.results)

    def _create_particle(self, mass, x, y, ttl):
        p = self.world.CreateDynamicBody(
            position=(x, y),
            angle=0.0,
            fixtures=fixtureDef(
                shape=circleShape(radius=2 / SCALE, pos=(0, 0)),
                density=mass,
                friction=0.1,
                categoryBits=0x0100,
                maskBits=0x001,  # collide only with ground
                restitution=0.3,
            ),
        )
        p.ttl = ttl
        self.particles.append(p)
        self._clean_particles(False)
        return p

    def _clean_particles(self, all_particle):
        while self.particles and (all_particle or self.particles[0].ttl < 0):
            self.world.DestroyBody(self.particles.pop(0))

    def step(self, action):
        print(self.results)
        assert self.lander is not None

        # Update wind and apply to the lander
        assert self.lander is not None, "You forgot to call reset()"
        if self.enable_wind and not (
            self.legs[0].ground_contact or self.legs[1].ground_contact
        ):
            # the function used for wind is tanh(sin(2 k x) + sin(pi k x)),
            # which is proven to never be periodic, k = 0.01
            wind_mag = (
                math.tanh(
                    math.sin(0.02 * self.wind_idx)
                    + (math.sin(math.pi * 0.01 * self.wind_idx))
                )
                * self.wind_power
            )
            self.wind_idx += 1
            self.lander.ApplyForceToCenter(
                (wind_mag, 0.0),
                True,
            )

            # the function used for torque is tanh(sin(2 k x) + sin(pi k x)),
            # which is proven to never be periodic, k = 0.01
            torque_mag = (
                math.tanh(
                    math.sin(0.02 * self.torque_idx)
                    + (math.sin(math.pi * 0.01 * self.torque_idx))
                )
                * self.turbulence_power
            )
            self.torque_idx += 1
            self.lander.ApplyTorque(
                torque_mag,
                True,
            )

        if self.continuous:
            action = np.clip(action, -1, +1).astype(np.float64)
        else:
            assert self.action_space.contains(
                action
            ), f"{action!r} ({type(action)}) invalid "

        # print(self.lander.position)
        position = tuple(self.lander.position)  # Replace with real position
        velocity = tuple(self.lander.linearVelocity)  # Replace with real velocity
        angle = self.lander.angle  # Replace with real angle
        # TODO: what is acceleration
        inertia = self.lander.inertia  # Replace with real acceleration

        # Tip is the (X and Y) components of the rotation of the lander.
        tip = (math.sin(self.lander.angle), math.cos(self.lander.angle))

        # Side is the (-Y and X) components of the rotation of the lander.
        side = (-tip[1], tip[0])

        # Generate two random numbers between -1/SCALE and 1/SCALE.
        dispersion = [self.np_random.uniform(-1.0, +1.0) / SCALE for _ in range(2)]

        m_power = 0.0
        if (self.continuous and action[0] > 0.0) or (
            not self.continuous and action == 2
        ):
            # Main engine
            if self.continuous:
                m_power = (np.clip(action[0], 0.0, 1.0) + 1.0) * 0.5  # 0.5..1.0
                assert m_power >= 0.5 and m_power <= 1.0
            else:
                m_power = 1.0

            # 4 is move a bit downwards, +-2 for randomness
            # The components of the impulse to be applied by the main engine.
            ox = (
                tip[0] * (MAIN_ENGINE_Y_LOCATION / SCALE + 2 * dispersion[0])
                + side[0] * dispersion[1]
            )
            oy = (
                -tip[1] * (MAIN_ENGINE_Y_LOCATION / SCALE + 2 * dispersion[0])
                - side[1] * dispersion[1]
            )

            impulse_pos = (self.lander.position[0] + ox, self.lander.position[1] + oy)
            if self.render_mode is not None:
                # particles are just a decoration, with no impact on the physics, so don't add them when not rendering
                p = self._create_particle(
                    3.5,  # 3.5 is here to make particle speed adequate
                    impulse_pos[0],
                    impulse_pos[1],
                    m_power,
                )
                p.ApplyLinearImpulse(
                    (
                        ox * MAIN_ENGINE_POWER * m_power,
                        oy * MAIN_ENGINE_POWER * m_power,
                    ),
                    impulse_pos,
                    True,
                )
            self.lander.ApplyLinearImpulse(
                (-ox * MAIN_ENGINE_POWER * m_power, -oy * MAIN_ENGINE_POWER * m_power),
                impulse_pos,
                True,
            )

        s_power = 0.0
        if (self.continuous and np.abs(action[1]) > 0.5) or (
            not self.continuous and action in [1, 3]
        ):
            # Orientation/Side engines
            if self.continuous:
                direction = np.sign(action[1])
                s_power = np.clip(np.abs(action[1]), 0.5, 1.0)
                assert s_power >= 0.5 and s_power <= 1.0
            else:
                # action = 1 is left, action = 3 is right
                direction = action - 2
                s_power = 1.0

            # The components of the impulse to be applied by the side engines.
            ox = tip[0] * dispersion[0] + side[0] * (
                3 * dispersion[1] + direction * SIDE_ENGINE_AWAY / SCALE
            )
            oy = -tip[1] * dispersion[0] - side[1] * (
                3 * dispersion[1] + direction * SIDE_ENGINE_AWAY / SCALE
            )

            # The constant 17 is a constant, that is presumably meant to be SIDE_ENGINE_HEIGHT.
            # However, SIDE_ENGINE_HEIGHT is defined as 14
            # This causes the position of the thrust on the body of the lander to change, depending on the orientation of the lander.
            # This in turn results in an orientation dependent torque being applied to the lander.
            impulse_pos = (
                self.lander.position[0] + ox - tip[0] * 17 / SCALE,
                self.lander.position[1] + oy + tip[1] * SIDE_ENGINE_HEIGHT / SCALE,
            )
            if self.render_mode is not None:
                # particles are just a decoration, with no impact on the physics, so don't add them when not rendering
                p = self._create_particle(0.7, impulse_pos[0], impulse_pos[1], s_power)
                p.ApplyLinearImpulse(
                    (
                        ox * SIDE_ENGINE_POWER * s_power,
                        oy * SIDE_ENGINE_POWER * s_power,
                    ),
                    impulse_pos,
                    True,
                )
            self.lander.ApplyLinearImpulse(
                (-ox * SIDE_ENGINE_POWER * s_power, -oy * SIDE_ENGINE_POWER * s_power),
                impulse_pos,
                True,
            )

        self.world.Step(1.0 / FPS, 6 * 30, 2 * 30)

        pos = self.lander.position
        vel = self.lander.linearVelocity

        state = [
            (pos.x - VIEWPORT_W / SCALE / 2) / (VIEWPORT_W / SCALE / 2),
            (pos.y - (self.helipad_y + LEG_DOWN / SCALE)) / (VIEWPORT_H / SCALE / 2),
            vel.x * (VIEWPORT_W / SCALE / 2) / FPS,
            vel.y * (VIEWPORT_H / SCALE / 2) / FPS,
            self.lander.angle,
            20.0 * self.lander.angularVelocity / FPS,
            1.0 if self.legs[0].ground_contact else 0.0,
            1.0 if self.legs[1].ground_contact else 0.0,
        ]
        assert len(state) == 8

        reward = 0
        shaping = (
            self.shaping_0 * np.sqrt(state[0] * state[0] + state[1] * state[1])
            - self.shaping_1 * np.sqrt(state[2] * state[2] + state[3] * state[3])
            - self.shaping_2 * abs(state[4])
            + self.shaping_3 * state[6]
            + self.shaping_4 * state[7]
        )  # And ten points for legs contact, the idea is if you
        # lose contact again after landing, you get negative reward
        if self.prev_shaping is not None:
            reward = shaping - self.prev_shaping
        self.prev_shaping = shaping

        reward -= (
            m_power * self.m_power_coef
        )  # less fuel spent is better, about -30 for heuristic landing
        reward -= s_power * 0.03

        terminated = False
        if self.game_over or abs(state[0]) >= 1.0:
            terminated = True
            reward = -100
        if not self.lander.awake:
            terminated = True
            reward = +100

        # Log the state
        print(self.results)
        self.results["intermediate_states"].append({
            "time_step": len(self.results["intermediate_states"]) + 1,
            "position": position,
            "velocity": velocity,
            "angle": angle,
            "inertia": inertia,
        })


        if self.render_mode == "human":
            self.render()

        # truncation=False as the time limit is handled by the `TimeLimit` wrapper added during `make`
        return np.array(state, dtype=np.float32), reward, terminated, False, {}

    def render(self):
        if self.render_mode is None:
            assert self.spec is not None
            gym.logger.warn(
                "You are calling render method without specifying any render mode. "
                "You can specify the render_mode at initialization, "
                f'e.g. gym.make("{self.spec.id}", render_mode="rgb_array")'
            )
            return

        try:
            import pygame
            from pygame import gfxdraw
        except ImportError as e:
            raise DependencyNotInstalled(
                'pygame is not installed, run `pip install "gymnasium[box2d]"`'
            ) from e

        if self.screen is None and self.render_mode == "human":
            pygame.init()
            pygame.display.init()
            self.screen = pygame.display.set_mode((VIEWPORT_W, VIEWPORT_H))
        if self.clock is None:
            self.clock = pygame.time.Clock()

        self.surf = pygame.Surface((VIEWPORT_W, VIEWPORT_H))

        pygame.transform.scale(self.surf, (SCALE, SCALE))
        pygame.draw.rect(self.surf, (255, 255, 255), self.surf.get_rect())

        for obj in self.particles:
            obj.ttl -= 0.15
            obj.color1 = (
                int(max(0.2, 0.15 + obj.ttl) * 255),
                int(max(0.2, 0.5 * obj.ttl) * 255),
                int(max(0.2, 0.5 * obj.ttl) * 255),
            )
            obj.color2 = (
                int(max(0.2, 0.15 + obj.ttl) * 255),
                int(max(0.2, 0.5 * obj.ttl) * 255),
                int(max(0.2, 0.5 * obj.ttl) * 255),
            )

        self._clean_particles(False)

        for p in self.sky_polys:
            scaled_poly = []
            for coord in p:
                scaled_poly.append((coord[0] * SCALE, coord[1] * SCALE))
            pygame.draw.polygon(self.surf, (0, 0, 0), scaled_poly)
            gfxdraw.aapolygon(self.surf, scaled_poly, (0, 0, 0))

        for obj in self.particles + self.drawlist:
            for f in obj.fixtures:
                trans = f.body.transform
                if type(f.shape) is circleShape:
                    pygame.draw.circle(
                        self.surf,
                        color=obj.color1,
                        center=trans * f.shape.pos * SCALE,
                        radius=f.shape.radius * SCALE,
                    )
                    pygame.draw.circle(
                        self.surf,
                        color=obj.color2,
                        center=trans * f.shape.pos * SCALE,
                        radius=f.shape.radius * SCALE,
                    )

                else:
                    path = [trans * v * SCALE for v in f.shape.vertices]
                    pygame.draw.polygon(self.surf, color=obj.color1, points=path)
                    gfxdraw.aapolygon(self.surf, path, obj.color1)
                    pygame.draw.aalines(
                        self.surf, color=obj.color2, points=path, closed=True
                    )

                for x in [self.helipad_x1, self.helipad_x2]:
                    x = x * SCALE
                    flagy1 = self.helipad_y * SCALE
                    flagy2 = flagy1 + 50
                    pygame.draw.line(
                        self.surf,
                        color=(255, 255, 255),
                        start_pos=(x, flagy1),
                        end_pos=(x, flagy2),
                        width=1,
                    )
                    pygame.draw.polygon(
                        self.surf,
                        color=(204, 204, 0),
                        points=[
                            (x, flagy2),
                            (x, flagy2 - 10),
                            (x + 25, flagy2 - 5),
                        ],
                    )
                    gfxdraw.aapolygon(
                        self.surf,
                        [(x, flagy2), (x, flagy2 - 10), (x + 25, flagy2 - 5)],
                        (204, 204, 0),
                    )

        self.surf = pygame.transform.flip(self.surf, False, True)

        if self.render_mode == "human":
            assert self.screen is not None
            self.screen.blit(self.surf, (0, 0))
            pygame.event.pump()
            self.clock.tick(self.metadata["render_fps"])
            pygame.display.flip()
        elif self.render_mode == "rgb_array":
            return np.transpose(
                np.array(pygame.surfarray.pixels3d(self.surf)), axes=(1, 0, 2)
            )

    def close(self):
        if self.screen is not None:
            import pygame

            pygame.display.quit()
            pygame.quit()
            self.isopen = False

    def save_results(self, output_path: str = "output.json"):
        """Save the results of the experiment to a JSON file."""
        with open(output_path, "w") as f:
            json.dump(self.results, f, indent=4)

def save_gif(pixel_arrays, output_gif_path, duration=100):
    """
    Creates a GIF from a list of NumPy pixel arrays.

    Args:
        pixel_arrays: A list of 2D or 3D NumPy arrays representing image frames.
                      2D arrays are assumed to be grayscale, 3D arrays are RGB(A).
        output_gif_path: The path where the output GIF will be saved.
        duration: The duration of each frame in milliseconds.
    """
    try:
        frames = []
        for pixel_array in pixel_arrays:
            try:
                # Convert to NumPy array if necessary
                if not isinstance(pixel_array, np.ndarray):
                    pixel_array = np.array(pixel_array)


                # Automatically determine mode based on array dimensions
                if pixel_array.ndim == 2:  # Grayscale
                    img = Image.fromarray(pixel_array.astype(np.uint8), mode="L")

                elif pixel_array.ndim == 3:  # RGB or RGBA
                    if pixel_array.shape[2] == 3:
                        img = Image.fromarray(pixel_array.astype(np.uint8), mode="RGB")

                    elif pixel_array.shape[2] == 4:
                        img = Image.fromarray(pixel_array.astype(np.uint8), mode="RGBA")
                    else:
                        raise ValueError("3D pixel arrays must have 3 (RGB) or 4 (RGBA) channels in the last dimension.")
                else:
                    raise ValueError("Pixel arrays must be 2D (grayscale) or 3D (RGB/RGBA).")




                frames.append(img)
            except ValueError as e: # Catch array dimension issues
                print(f"Error processing pixel array: {e}")
                return
            except Exception as e:
                print(f"Error processing image: {e}")
                return


        if frames:  # Make GIF only if there are valid frames
            frames[0].save(
                output_gif_path,
                save_all=True,
                append_images=frames[1:],
                optimize=False,
                duration=duration,
                loop=0,
            )
            print(f"GIF created successfully: {output_gif_path}")
        else:
            print("No valid image data to create GIF.")



    except Exception as e:
        print(f"An error occurred: {e}")

def heuristic(env, s):
    """
    The heuristic for
    1. Testing
    2. Demonstration rollout.

    Args:
        env: The environment
        s (list): The state. Attributes:
            s[0] is the horizontal coordinate
            s[1] is the vertical coordinate
            s[2] is the horizontal speed
            s[3] is the vertical speed
            s[4] is the angle
            s[5] is the angular speed
            s[6] 1 if first leg has contact, else 0
            s[7] 1 if second leg has contact, else 0

    Returns:
         a: The heuristic to be fed into the step function defined above to determine the next step and reward.
    """
    angle_targ = s[0] * 0.5 + s[2] * 1.0  # angle should point towards center
    if angle_targ > 0.4:
        angle_targ = 0.4  # more than 0.4 radians (22 degrees) is bad
    if angle_targ < -0.4:
        angle_targ = -0.4
    hover_targ = 0.55 * np.abs(
        s[0]
    )  # target y should be proportional to horizontal offset

    angle_todo = (angle_targ - s[4]) * 0.5 - (s[5]) * 1.0
    hover_todo = (hover_targ - s[1]) * 0.5 - (s[3]) * 0.5

    if s[6] or s[7]:  # legs have contact
        angle_todo = 0
        hover_todo = (
            -(s[3]) * 0.5
        )  # override to reduce fall speed, that's all we need after contact

    if env.unwrapped.continuous:
        a = np.array([hover_todo * 20 - 1, -angle_todo * 20])
        a = np.clip(a, -1, +1)
    else:
        a = 0
        if hover_todo > np.abs(angle_todo) and hover_todo > 0.05:
            a = 2
        elif angle_todo < -0.05:
            a = 3
        elif angle_todo > +0.05:
            a = 1
    return a


frames = []
def demo_heuristic_lander(env, seed=None, render=False):
    total_reward = 0
    steps = 0
    s, info = env.reset(seed=seed)
    while True:
        a = heuristic(env, s)
        s, r, terminated, truncated, info = step_api_compatibility(env.step(a), True)
        total_reward += r

        frames.append(env.render())

        if render:
            still_open = env.render()
            if still_open is False:
                break

        if steps % 20 == 0 or terminated or truncated:
            print("observations:", " ".join([f"{x:+0.2f}" for x in s]))
            print(f"step {steps} total_reward {total_reward:+0.2f}")
        steps += 1
        if terminated or truncated:
            break

    env.save_results("output.json")
    save_gif(frames, "simulation.gif")

    if render:
        env.close()
    return total_reward


class LunarLanderContinuous:
    def __init__(self):
        raise error.Error(
            "Error initializing LunarLanderContinuous Environment.\n"
            "Currently, we do not support initializing this mode of environment by calling the class directly.\n"
            "To use this environment, instead create it by specifying the continuous keyword in gym.make, i.e.\n"
            'gym.make("LunarLander-v3", continuous=True)'
        )


if __name__ == "__main__":
    with open('input.json', 'r') as file:
        params = json.load(file)

    lander = LunarLander(config_path="input.json", experiment_number=1)
    # env = gym.make("LunarLander-v3",
    #                render_mode=params["main"]["render_mode"],
    #                continuous=params["main"]["continuous"],
    #                gravity=params["main"]["gravity"],
    #                enable_wind=params["main"]["enable_wind"],
    #                wind_power=params["main"]["wind_power"],
    #                turbulence_power=params["main"]["turbulence_power"])

    demo_heuristic_lander(lander, render=False)
