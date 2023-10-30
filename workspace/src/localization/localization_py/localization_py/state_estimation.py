import csv
import rclpy
from rclpy.node import Node
from art_msgs.msg import VehicleState, VehicleInput
from sensor_msgs.msg import Imu, NavSatFix, MagneticField
from chrono_ros_msgs.msg import ChVehicle
from ament_index_python.packages import get_package_share_directory
import matplotlib.pyplot as plt
import matplotlib
import math
import numpy as np
import sys
import os
from enum import Enum
from localization_py.EKF import EKF
from localization_py.particle_filter import ParticleFilter as PF
from localization_py.chrono_coordinate_transfer import Graph


class EstimationAlgorithmOption(Enum):
    GROUND_TRUTH = "ground_truth"
    EXTENDED_KALMAN_FILTER = "extended_kalman_filter"
    PARTICLE_FILTER = "particle_filter"


class StateEstimationNode(Node):
    def __init__(self):
        super().__init__("state_estimation_node")

        # ROS PARAMETERS
        self.use_sim_msg = (
            self.get_parameter("use_sim_time").get_parameter_value().bool_value
        )

        self.declare_parameter("estimation_alg", EstimationAlgorithmOption.GROUND_TRUTH)
        self.estimation_alg = (
            self.get_parameter("estimation_alg").get_parameter_value().string_value
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
        self.get_logger().info(str(dyn))

        # update frequency of this node
        self.freq = 10.0

        self.gps = ""
        self.ground_truth = ""
        self.mag = ""

        # x, y, from measurements
        self.x = 0
        self.y = 0

        # what we will be using for our state vector. (x, y, theta yaw, v vel)
        self.state = np.zeros((4, 1))
        np.vstack(self.state)

        self.init_x = 0.0
        self.init_y = 0.0
        self.init_theta = 0.0
        self.init_v = 0.0
        self.state[0, 0] = self.init_x
        self.state[1, 0] = self.init_y
        self.state[2, 0] = self.init_theta
        self.state[3, 0] = self.init_v

        self.gps_ready = False

        # true X, True Y, velocity, z, heading(degrees)
        self.gtvy = 0
        self.gty = 0
        self.gtvx = 0
        self.gtx = 0
        self.gtz = 0
        self.D = 0

        # origin, and whether or not the origin has been set yet.
        self.origin_set = False
        self.orig_heading_set = False

        # inputs to the vehicle
        self.throttle = 0.0
        self.steering = 0

        # time between imu updates, sec
        self.dt_gps = 1 / self.freq

        # filter
        if self.estimation_alg == EstimationAlgorithmOption.EXTENDED_KALMAN_FILTER:
            self.ekf = EKF(self.dt_gps, dyn, Q, R)
        elif self.estimation_alg == EstimationAlgorithmOption.PARTICLE_FILTER:
            self.pf = PF(self.dt_gps, dyn)

        # our graph object, for reference frame
        self.graph = Graph()

        # subscribers
        self.sub_gps = self.create_subscription(
            NavSatFix, "~/input/gps", self.gps_callback, 1
        )
        if self.use_sim_msg:
            self.sub_groud_truth = self.create_subscription(
                ChVehicle, "~/input/ground_truth", self.ground_truth_callback, 1
            )

        self.sub_mag = self.create_subscription(
            MagneticField, "~/input/magnetometer", self.mag_callback, 1
        )
        self.sub_control = self.create_subscription(
            VehicleInput, "~/input/vehicle_inputs", self.inputs_callback, 1
        )
        # publishers
        self.pub_objects = self.create_publisher(
            VehicleState, "~/output/vehicle/filtered_state", 1
        )
        self.timer = self.create_timer(1 / self.freq, self.pub_callback)

    # CALLBACKS:
    def inputs_callback(self, msg):
        self.inputs = msg
        self.steering = self.inputs.steering
        self.throttle = self.inputs.throttle

    def ground_truth_callback(self, msg):
        self.gtx = msg.pose.position.x
        self.gty = msg.pose.position.y
        self.gtvx = msg.twist.linear.x
        self.gtvy = msg.twist.linear.y

    def mag_callback(self, msg):
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
            self.x, self.y, self.z = self.graph.rotate(x, y, z)
            self.x += self.init_x
            self.y += self.init_y

    # callback to run a loop and publish data this class generates
    def pub_callback(self):
        u = np.array([[self.throttle], [self.steering / 2.2]])

        z = np.array([[self.x], [self.y], [np.deg2rad(self.D)]])

        if self.estimation_alg == EstimationAlgorithmOption.EXTENDED_KALMAN_FILTER:
            self.EKFstep(u, z)
        elif self.estimation_alg == EstimationAlgorithmOption.PARTICLE_FILTER:
            self.PFstep(u, z)

        msg = VehicleState()
        # pos and velocity are in meters, from the origin, [x, y, z]
        if self.estimation_alg == EstimationAlgorithmOption.GROUND_TRUTH:
            msg.pose.position.x = float(self.gtx)
            msg.pose.position.y = float(self.gty)
            # TODO: this should be a quat in the future, not the heading.
            msg.pose.orientation.z = np.deg2rad(self.D)
            msg.twist.linear.x = float(self.gtvx)
            msg.twist.linear.y = float(self.gtvy)
        else:
            msg.pose.position.x = float(self.state[0, 0])
            msg.pose.position.y = float(self.state[1, 0])
            msg.pose.orientation.z = float(self.state[2, 0])
            msg.twist.linear.x = float(self.state[3, 0] * math.cos(self.state[2, 0]))
            msg.twist.linear.y = float(self.state[3, 0] * math.sin(self.state[2, 0]))

        msg.header.stamp = self.get_clock().now().to_msg()
        self.pub_objects.publish(msg)

    def EKFstep(self, u, z):
        self.state = self.ekf.predict(self.state, u)
        if self.gps_ready:
            self.state = self.ekf.correct(self.state, z)
            self.gps_ready = False

    def PFstep(self, u, z):
        self.state = self.pf.update(u, z)


def main(args=None):
    print("=== Starting State Estimation Node ===")
    rclpy.init(args=args)
    estimator = StateEstimationNode()
    rclpy.spin(estimator)
    estimator.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
