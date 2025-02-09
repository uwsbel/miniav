import csv
import rclpy
from rclpy.node import Node
from art_msgs.msg import VehicleState
from chrono_ros_interfaces.msg import DriverInputs as VehicleInput
from sensor_msgs.msg import Imu, NavSatFix, MagneticField
from chrono_ros_interfaces.msg import Body as ChVehicle
import matplotlib.pyplot as plt
import matplotlib
import math
import numpy as np
import sys
import os
from enum import Enum
from ekf_estimation.EKF import EKF
from localization_shared_utils import get_dynamics, get_coordinate_transfer


class EKFEstimationNode(Node):
    """A state estimation node based on an Extended Kalman Filter.

    This Extended Kalman Filter is designed based on a 4 Degree of Freedom (DOF) dynamics, as defined in the `EKF.py` file and the `../../localization_shared_utils/localization_shared_utils/dynamics.py` files. Parameters for the filter are passed as parameters from a `.yaml` file.
    Attributes:
        Q{1-4}: The diagonal parameters for the 4x4 Q matrix for the EKF.
        R{1-3}: The diagonal parameters for the 3x3 R matrix for the EKF.
        c_1, c_0, l, r_wheel, i_wheel, gamma, tau_0, omega_0: Parameters for the dynamics of the ART vehicle.
        x, y: The Local Tangent Plane (LTP) - translated GPS coordinates.
        init_x, init_y, init_theta: Initialization data for the definition of the local tangent plane on which the vehicle is assumed to drive.
        state: The 4 DOF state of the vehicle, as defined by it's x and y coordinates, heading angle, and speed.
        throttle: The input throttle.
        steering: The input steering.
        gps: The observation of the position as a GPS reading.
        mag: The observation of the heading as a Magnetometer reading.origin_set: whether or not the origin and orientation of the LTP has been set.
        origin_heading_set: whether or not the original heading has been determined.
    """

    def __init__(self):
        """Initialize the Extended Kalman Filter node.

        Initialize the Extended Kalman Filter object with the appropriate parameters, and set the initial state of the vehicle. Subscribe to the GPS and Magnetometer topics, as well as the vehicle inputs. Set put the publisher to publish to the `filtered_state` topic.
        """
        super().__init__("ekf_estimation_node")

        # ROS PARAMETERS
        self.use_sim_msg = (
            self.get_parameter("use_sim_time").get_parameter_value().bool_value
        )
        self.declare_parameter("ekf_vel_only", False)
        self.ekf_vel_only = (
            self.get_parameter("ekf_vel_only").get_parameter_value().bool_value
        )

        # EKF parameters
        self.declare_parameter("Q1", 0.1)
        Q1 = self.get_parameter("Q1").get_parameter_value().double_value
        self.declare_parameter("Q3", 3)
        Q3 = self.get_parameter("Q3").get_parameter_value().double_value
        self.declare_parameter("Q4", 0.1)
        Q4 = self.get_parameter("Q4").get_parameter_value().double_value
        self.declare_parameter("R1", 0.0)
        R1 = self.get_parameter("R1").get_parameter_value().double_value
        self.declare_parameter("R3", 0.3)
        R3 = self.get_parameter("R3").get_parameter_value().double_value
        Q = [Q1, Q1, Q3, Q4]
        R = [R1, R1, R3]

        # dynamics parameters
        self.declare_parameter("c_1", 0.0001)
        c_1 = self.get_parameter("c_1").get_parameter_value().double_value
        self.declare_parameter("c_0", 0.02)
        c_0 = self.get_parameter("c_0").get_parameter_value().double_value
        self.declare_parameter("l", 0.5)
        l = self.get_parameter("l").get_parameter_value().double_value
        self.declare_parameter("r_wheel", 0.08451952624)
        r_wheel = self.get_parameter("r_wheel").get_parameter_value().double_value
        self.declare_parameter("i_wheel", 0.001)
        i_wheel = self.get_parameter("i_wheel").get_parameter_value().double_value
        self.declare_parameter("gamma", 0.33333333)
        gamma = self.get_parameter("gamma").get_parameter_value().double_value
        self.declare_parameter("tau_0", 0.3)
        tau_0 = self.get_parameter("tau_0").get_parameter_value().double_value
        self.declare_parameter("omega_0", 30.0)
        omega_0 = self.get_parameter("omega_0").get_parameter_value().double_value
        dyn = [c_1, c_0, l, r_wheel, i_wheel, gamma, tau_0, omega_0]

        # update frequency of this node
        self.freq = 10.0

        self.gps = ""
        self.mag = ""

        # x, y, from measurements
        self.x = 0
        self.y = 0

        # what we will be using for our state vector. (x, y, theta yaw, v vel)
        self.state = np.zeros((4, 1))

        self.init_x = 0.0
        self.init_y = 0.0
        self.init_theta = 0.0
        self.init_v = 0.0
        self.state[0, 0] = self.init_x
        self.state[1, 0] = self.init_y
        self.state[2, 0] = self.init_theta
        self.state[3, 0] = self.init_v

        self.gps_ready = False

        # ground truth velocity
        self.gtvy = 0
        self.gtvx = 0
        self.D = 0

        # origin, and whether or not the origin has been set yet.
        self.origin_set = False
        self.orig_heading_set = False

        # inputs to the vehicle
        self.throttle = 0.0
        self.steering = 0

        # time between imu updates, sec
        self.dt_gps = 1 / self.freq

        # the ROM
        self.dynamics_model = get_dynamics(self.dt_gps, dyn)
        # filter
        self.ekf = EKF(self.dt_gps, self.dynamics_model, Q, R)

        # our graph object, for reference frame
        self.graph = get_coordinate_transfer()

        # subscribers
        self.sub_gps = self.create_subscription(
            NavSatFix, "~/input/gps", self.gps_callback, 1
        )

        self.sub_mag = self.create_subscription(
            MagneticField, "~/input/magnetometer", self.mag_callback, 1
        )
        self.sub_control = self.create_subscription(
            VehicleInput, "~/input/vehicle_inputs", self.inputs_callback, 1
        )
        # publishers
        self.pub_objects = self.create_publisher(
            VehicleState, "~/output/filtered_state", 1
        )
        self.timer = self.create_timer(1 / self.freq, self.pub_callback)

    # CALLBACKS:
    def inputs_callback(self, msg):
        """Callback for the vehicle input subscriber.

        Read the input for the vehicle from the topic.

        Args:
            msg: The message received from the topic
        """
        self.inputs = msg
        self.steering = self.inputs.steering
        self.throttle = self.inputs.throttle

    def mag_callback(self, msg):
        """Callback for the Magnetometer subscriber.

        Read the Magnetometer observation from the topic. Process this into a heading angle, and then set the rotation of the LTP if the original heading has not been set yet.

        Args:
            msg: The message received from the topic
        """
        self.mag = msg
        mag_x = self.mag.magnetic_field.x
        mag_y = self.mag.magnetic_field.y
        mag_z = self.mag.magnetic_field.z
        xGauss = mag_x * 0.48828125
        yGauss = mag_y * 0.4882815
        if xGauss == 0:
            if yGauss < 0:
                self.D = 0
            else:
                self.D = 90
        else:
            self.D = math.atan2(yGauss, xGauss) * 180 / math.pi
        while self.D > 360:
            self.D = self.D - 360
        while self.D < 0:
            self.D = self.D + 360

        if not self.orig_heading_set:
            self.orig_heading_set = True
            self.graph.set_rotation(np.deg2rad(self.D) - self.init_theta)
            self.state[2, 0] = self.init_theta

    def gps_callback(self, msg):
        """Callback for the GPS subscriber.

        Read the GPS observation from the topic. If the original heading has been set, initialize the LTP and set this point as the origin. If the origin has been set, project this gps coordinate onto the defined LTP using the graph object, and return that x and y coordinate.

        Args:
            msg: The message received from the topic
        """
        self.gps = msg
        self.gps_ready = True
        if math.isnan(self.gps.latitude):
            # arbitrary values for when we don't get any data (in reality)
            self.lat = -10
            self.lon = -10
            self.alt = -10
        else:
            self.lat = self.gps.latitude
            self.lon = self.gps.longitude
            self.alt = self.gps.altitude

        if not self.origin_set:
            self.origin_set = True
            self.graph.set_graph(self.lat, self.lon, self.alt)

        x, y, z = self.graph.gps2cartesian(self.lat, self.lon, self.alt)
        if self.orig_heading_set:
            newx, newy, newz = self.graph.rotate(x, y, z)
            self.gtvx, self.gtvy = (newx - self.x) / self.dt_gps, (
                newy - self.y
            ) / self.dt_gps
            self.x, self.y, self.z = newx, newy, newz
            self.x += self.init_x
            self.y += self.init_y

    # callback to run a loop and publish data this class generates
    def pub_callback(self):
        """Callback for the publisher.

        Get the vehicle input (u) and observation (z), and step the EKF using this information. Then, publish the estimated state to the `filtered_state` topic.
        """
        u = np.array([[self.throttle], [self.steering / 2.2]])

        z = np.array([[self.x], [self.y], [np.deg2rad(self.D)]])

        self.EKFstep(u, z)

        msg = VehicleState()
        # pos and velocity are in meters, from the origin, [x, y, z]

        if self.ekf_vel_only:
            msg.pose.position.x = float(self.x)
            msg.pose.position.y = float(self.y)
        else:
            msg.pose.position.x = float(self.state[0, 0])
            msg.pose.position.y = float(self.state[1, 0])
        msg.pose.orientation.z = float(self.state[2, 0])
        msg.twist.linear.x = float(self.state[3, 0] * math.cos(self.state[2, 0]))
        msg.twist.linear.y = float(self.state[3, 0] * math.sin(self.state[2, 0]))

        msg.header.stamp = self.get_clock().now().to_msg()
        self.pub_objects.publish(msg)

    def EKFstep(self, u, z):
        """Step the EKF.

        Propogate the EKF through the predict step using the vehicle input and through the correction step using the observation.

        Args:
            u: The vehicle throttle and steering input
            z: The location and heading observation
        """
        self.state = self.ekf.predict(self.state, u)
        if self.gps_ready:
            self.state = self.ekf.correct(self.state, z)
            self.gps_ready = False


def main(args=None):
    print("=== Starting State Estimation Node ===")
    rclpy.init(args=args)
    estimator = EKFEstimationNode()
    rclpy.spin(estimator)
    estimator.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
