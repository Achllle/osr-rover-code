#!/usr/bin/env python

import rospy
import math
import tf2_ros

from sensor_msgs.msg import JointState
from geometry_msgs.msg import Twist, TwistWithCovariance, TransformStamped
from nav_msgs.msg import Odometry
from osr_msgs.msg import CommandDrive, CommandCorner
from std_msgs.msg import Float64


class Rover(object):
    """Math and motor control algorithms to move the rover"""

    def __init__(self):
        rover_dimensions = rospy.get_param('/rover_dimensions', {"d1": 0.184, "d2": 0.267, "d3": 0.267, "d4": 0.256})
        self.d1 = rover_dimensions["d1"]
        self.d2 = rover_dimensions["d2"]
        self.d3 = rover_dimensions["d3"]
        self.d4 = rover_dimensions["d4"]

        self.min_radius = 0.45  # [m]
        self.max_radius = 6.4  # [m]

        self.no_cmd_thresh = 0.05  # [rad]
        self.wheel_radius = rospy.get_param("/rover_dimensions/wheel_radius", 0.075)  # [m]
        drive_no_load_rpm = rospy.get_param("/drive_no_load_rpm", 130)
        speed_adjustment_factor = rospy.get_param("/speed_adjustment_factor", 1.0)
        self.max_vel = self.wheel_radius * drive_no_load_rpm / 60 * 2 * math.pi * speed_adjustment_factor  # [m/s]
        self.odometry = Odometry()
        self.odometry.header.stamp = rospy.Time.now()
        self.odometry.header.frame_id = "map"
        self.odometry.child_frame_id = "odom"
        self.odometry.pose.pose.orientation.w = 1.
        self.curr_twist = TwistWithCovariance()
        self.curr_turning_radius = self.max_radius

        rospy.Subscriber("/cmd_vel", Twist, self.cmd_cb)
        rospy.Subscriber("/encoder", JointState, self.enc_cb)

        self.corner_cmd_pub = rospy.Publisher("/cmd_corner", CommandCorner, queue_size=1)
        self.drive_cmd_pub = rospy.Publisher("/cmd_drive", CommandDrive, queue_size=1)
        self.turning_radius_pub = rospy.Publisher("/turning_radius", Float64, queue_size=1)
        self.odometry_pub = rospy.Publisher("/odom", Odometry, queue_size=2)
        self.tf_pub = tf2_ros.TransformBroadcaster()

    def cmd_cb(self, twist_msg):
        desired_turning_radius = self.twist_to_turning_radius(twist_msg)
        rospy.logdebug("desired turning radius: {}".format(desired_turning_radius))
        corner_cmd_msg = self.calculate_corner_positions(desired_turning_radius)

        # if we're turning, calculate the max velocity the middle of the rover can go
        max_vel = abs(desired_turning_radius) / (abs(desired_turning_radius) + self.d1) * self.max_vel
        if math.isnan(max_vel):  # turning radius infinite, going straight
            max_vel = self.max_vel
        velocity = min(max_vel, twist_msg.linear.x)
        rospy.logdebug("velocity drive cmd: {} m/s".format(velocity))

        drive_cmd_msg = self.calculate_drive_velocities(velocity, self.curr_turning_radius)
        rospy.logdebug("drive cmd:\n{}".format(drive_cmd_msg))
        rospy.logdebug("corner cmd:\n{}".format(corner_cmd_msg)) 
        if self.corner_cmd_threshold(corner_cmd_msg):
            self.corner_cmd_pub.publish(corner_cmd_msg)
        self.drive_cmd_pub.publish(drive_cmd_msg)

    def enc_cb(self, msg):
        self.curr_positions = dict(zip(msg.name, msg.position))
        self.curr_velocities = dict(zip(msg.name, msg.velocity))
        # measure how much time has elapsed since our last update
        now = rospy.Time.now()
        dt = (now - self.odometry.header.stamp).to_sec()
        self.forward_kinematics()
        dx = self.curr_twist.twist.linear.x * dt
        dth = self.curr_twist.twist.angular.z * dt
        # angle is straightforward: in 2D it's additive
        self.odometry.pose.pose.orientation.z += dth
        # the new pose in x and y depends on the current heading
        heading = self.odometry.pose.pose.orientation.z
        self.odometry.pose.pose.position.x += math.cos(heading) * dx
        self.odometry.pose.pose.position.y += math.sin(heading) * dx
        self.odometry.pose.covariance = 36 * [0.03,]
        self.odometry.twist = self.curr_twist
        self.odometry.header.stamp = now
        self.odometry_pub.publish(self.odometry)
        transform_msg = TransformStamped()
        transform_msg.header.frame_id = "odom"
        transform_msg.child_frame_id = "base_link"
        transform_msg.header.stamp = now
        transform_msg.transform.translation.x = self.odometry.pose.pose.position.x
        transform_msg.transform.translation.y = self.odometry.pose.pose.position.y
        transform_msg.transform.rotation = self.odometry.pose.pose.orientation
        self.tf_pub.sendTransform(transform_msg)

    def corner_cmd_threshold(self, corner_cmd):
        try:
            if abs(corner_cmd.left_front_pos - self.curr_positions["corner_left_front"]) > self.no_cmd_thresh:
                return True
            elif abs(corner_cmd.left_back_pos - self.curr_positions["corner_left_back"]) > self.no_cmd_thresh:
                return True
            elif abs(corner_cmd.right_back_pos - self.curr_positions["corner_right_back"]) > self.no_cmd_thresh:
                return True
            elif abs(corner_cmd.right_front_pos - self.curr_positions["corner_right_front"]) > self.no_cmd_thresh:
                return True
            else:
                return False
        except AttributeError:  # haven't received current encoder positions yet
            return True

    def calculate_drive_velocities(self, speed, current_radius):
        """
        Calculate target velocities for the drive motors based on desired speed and current turning radius

        :param speed: Drive speed command range from -max_vel to max_vel, with max vel depending on the turning radius
        :param radius: Current turning radius in m
        """
        # clip the value to the maximum allowed velocity
        speed = max(-self.max_vel, min(self.max_vel, speed))
        cmd_msg = CommandDrive()
        if speed == 0:
            return cmd_msg

        elif abs(current_radius) >= self.max_radius:  # Very large turning radius, all wheels same speed
            angular_vel = speed / self.wheel_radius
            cmd_msg.left_front_vel = angular_vel
            cmd_msg.left_middle_vel = angular_vel
            cmd_msg.left_back_vel = angular_vel
            cmd_msg.right_back_vel = angular_vel
            cmd_msg.right_middle_vel = angular_vel
            cmd_msg.right_front_vel = angular_vel

            return cmd_msg

        else:
            # for the calculations, we assume positive radius (turn left) and adjust later
            radius = abs(current_radius)
            # the entire vehicle moves with the same angular velocity dictated by the desired speed,
            # around the radius of the turn. v = r * omega
            angular_velocity_center = float(speed) / radius
            # calculate desired velocities of all centers of wheels. Corner wheels on the same side
            # move with the same velocity. v = r * omega again
            vel_middle_closest = (radius - self.d4) * angular_velocity_center
            vel_corner_closest = math.hypot(radius - self.d1, self.d3) * angular_velocity_center
            vel_corner_farthest = math.hypot(radius + self.d1, self.d3) * angular_velocity_center
            vel_middle_farthest = (radius + self.d4) * angular_velocity_center

            # now from these desired velocities, calculate the desired angular velocity of each wheel
            # v = r * omega again
            ang_vel_middle_closest = vel_middle_closest / self.wheel_radius
            ang_vel_corner_closest = vel_corner_closest / self.wheel_radius
            ang_vel_corner_farthest = vel_corner_farthest / self.wheel_radius
            ang_vel_middle_farthest = vel_middle_farthest / self.wheel_radius

            if current_radius > 0:  # turning left
                cmd_msg.left_front_vel = ang_vel_corner_closest
                cmd_msg.left_back_vel = ang_vel_corner_closest
                cmd_msg.left_middle_vel = ang_vel_middle_closest
                cmd_msg.right_back_vel = ang_vel_corner_farthest
                cmd_msg.right_front_vel = ang_vel_corner_farthest
                cmd_msg.right_middle_vel = ang_vel_middle_farthest
            else:  # turning right
                cmd_msg.left_front_vel = ang_vel_corner_farthest
                cmd_msg.left_back_vel = ang_vel_corner_farthest
                cmd_msg.left_middle_vel = ang_vel_middle_farthest
                cmd_msg.right_back_vel = ang_vel_corner_closest
                cmd_msg.right_front_vel = ang_vel_corner_closest
                cmd_msg.right_middle_vel = ang_vel_middle_closest

            return cmd_msg

    def calculate_corner_positions(self, radius):
        """
        Takes a turning radius and computes the required angle for each corner motor

        A small turning radius means a sharp turn
        A large turning radius means mostly straight. Any radius larger than max_radius is essentially straight
        because of the encoders' resolution

        :param radius: positive value means turn left. 0.45 < abs(turning_radius) < inf
        """
        cmd_msg = CommandCorner()

        if radius >= self.max_radius:
            return cmd_msg  # assume straight

        theta_front_closest = math.atan2(self.d3, abs(radius) - self.d1)
        theta_front_farthest = math.atan2(self.d3, abs(radius) + self.d1)

        if radius > 0:
            cmd_msg.left_front_pos = theta_front_closest
            cmd_msg.left_back_pos = -theta_front_closest
            cmd_msg.right_back_pos = -theta_front_farthest
            cmd_msg.right_front_pos = theta_front_farthest
        else:
            cmd_msg.left_front_pos = -theta_front_farthest
            cmd_msg.left_back_pos = theta_front_farthest
            cmd_msg.right_back_pos = theta_front_closest
            cmd_msg.right_front_pos = -theta_front_closest

        return cmd_msg

    def twist_to_turning_radius(self, twist, clip=True):
        """
        Convert a commanded twist into an actual turning radius

        ackermann steering: if l is distance travelled, rho the turning radius, and theta the heading of the middle of the robot,
        then: dl = rho * dtheta. With dt -> 0, dl/dt = rho * dtheta/dt
        dl/dt = twist.linear.x, dtheta/dt = twist.angular.z

        :param twist: geometry_msgs/Twist. Only linear.x and angular.z are used
        :param clip: whether the values should be clipped from min_radius to max_radius
        :return: physical turning radius in meter, clipped to the rover's limits
        """
        try:
            radius = twist.linear.x / twist.angular.z
            if radius == 0:
                return float("Inf")
        except ZeroDivisionError:
            return float("Inf")
        
        # clip values so they lie in (-max_radius, -min_radius) or (min_radius, max_radius)
        if not clip:
            return radius
        if radius > 0:
            radius = max(self.min_radius, min(self.max_radius, radius))
        else:
            radius = max(-self.max_radius, min(-self.min_radius, radius))

        return radius

    def angle_to_turning_radius(self, angle):
        """
        Convert the angle of a virtual wheel positioned in the middle of the front two wheels to a turning radius
        Turning left and positive angle corresponds to a positive turning radius

        :param angle: [-pi/4, pi/4]
        :return: turning radius for the given angle in [m]
        """
        try:
            radius = self.d3 / math.tan(angle)
        except ZeroDivisionError:
            return float("Inf")

        return radius

    def forward_kinematics(self):
        """
        Calculate current twist of the rover given current drive and corner motor velocities
        Also approximate current turning radius.

        Note that forward kinematics means solving an overconstrained system since the corner 
        motors may not be aligned perfectly and drive velocities might fight each other
        """
        # calculate current turning radius according to each corner wheel's angle
        theta_fl = self.curr_positions['corner_left_front']
        theta_fr = self.curr_positions['corner_right_front']
        theta_bl = self.curr_positions['corner_left_back']
        theta_br = self.curr_positions['corner_right_back']
        # sum wheel angles to find out which direction the rover is mostly turning in
        if theta_fl + theta_fr + theta_bl + theta_br > 0:  # turning left
            r_front_closest = self.d1 + self.angle_to_turning_radius(theta_fl)
            r_front_farthest = -self.d1 + self.angle_to_turning_radius(theta_fr)
            r_back_closest = -self.d1 - self.angle_to_turning_radius(theta_bl)
            r_back_farthest = self.d1 - self.angle_to_turning_radius(theta_br)
        else:  # turning right
            r_front_farthest = self.d1 + self.angle_to_turning_radius(theta_fl)
            r_front_closest = -self.d1 + self.angle_to_turning_radius(theta_fr)
            r_back_farthest = -self.d1 - self.angle_to_turning_radius(theta_bl)
            r_back_closest = self.d1 - self.angle_to_turning_radius(theta_br)
        # get a best estimate of the turning radius by taking the median value (avg sensitive to outliers)
        approx_turning_radius = sum(sorted([r_front_farthest, r_front_closest, r_back_farthest, r_back_closest])[1:3])/2.0
        if math.isnan(approx_turning_radius):
            approx_turning_radius = float("Inf")
        if abs(approx_turning_radius) < self.max_radius:
            self.turning_radius_pub.publish(Float64(approx_turning_radius))
        else:
            self.turning_radius_pub.publish(Float64(self.max_radius))
        rospy.logdebug_throttle(0.5, "Current approximate turning radius: {}".format(round(approx_turning_radius, 2)))
        self.curr_turning_radius = approx_turning_radius

        # we know that the linear velocity in x direction is the instantaneous velocity of the middle virtual
        # wheel which spins at the average speed of the two middle outer wheels.
        self.curr_twist.twist.linear.x = (self.curr_velocities['drive_left_middle'] + self.curr_velocities['drive_right_middle']) / 2.
        # now calculate angular velocity from its relation with linear velocity and turning radius
        self.curr_twist.twist.angular.z = self.curr_twist.twist.linear.x / self.curr_turning_radius
        # covariance
        self.curr_twist.covariance = 36 * [0.02,]

if __name__ == '__main__':
    rospy.init_node('rover', log_level=rospy.INFO)
    rospy.loginfo("Starting the rover node")
    rover = Rover()
    rospy.spin()
