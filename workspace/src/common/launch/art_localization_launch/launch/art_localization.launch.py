# ros imports
from launch import LaunchDescription

# internal imports
from launch_utils import IncludeLaunchDescriptionWithCondition
from launch_utils import AddLaunchArgument, GetLaunchArgument, GetPackageSharePath
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch.actions import DeclareLaunchArgument


def generate_launch_description():
    ld = LaunchDescription()

    # ---------------
    # Launch Includes
    # ---------------
    
    
    IncludeLaunchDescriptionWithCondition(
        ld, "art_localization_launch", "chrono_imu_filter"
    )

    IncludeLaunchDescriptionWithCondition(ld, "art_localization_launch", "ground_truth")


    IncludeLaunchDescriptionWithCondition(
        ld, "art_localization_launch", "ekf_estimation"
    )
    IncludeLaunchDescriptionWithCondition(
        ld, "art_localization_launch", "particle_filter_estimation"
    )    
    IncludeLaunchDescriptionWithCondition(ld, "art_localization_launch", "ekf_launch")

    return ld
