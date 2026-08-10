from dobot_util import Dobot, DobotError
from time import sleep

if __name__ == "__main__":
    # Provide the IP of the Dobot Robot, determine if you want to log or not - read other default arguments and determine what you want to change.
    robot = Dobot("192.168.1.6", logging=True)

    # Attempt to turn on the robot with the needed command, could fail - but we assume it works.
    robot.dashboard.enable()

    # This is a joint to joint move, so you provide the joint values you want to move to.
    # The amount of joints needed to pass depends on the robot model type.
    # For the case of an M1 Pro, it would need to be four floats.

    # Most if not all of the API commands could potentially fail with an error which means YOU need to handle errors.
    # It is up to the user to determine how to go about errors, ignore them, try to redo the command, or even just panic and exit.
    # Until positioning is integrated into the move commands, you typically need to sleep between commands or check positioning yourself.

    # Go to strawberry
    robot.movement.joint_to_joint_move([60.0, 82.0, 240.0, -35])
    sleep(1)
    robot.movement.joint_to_joint_move([60.0, 82.0, 55.0, -35])
    sleep(3)

    # Pick up strawberry
    robot.dashboard.set_digital_output(1, 1)
    sleep(1)

    # Go to0 designated location
    robot.movement.joint_to_joint_move([60.0, 82.0, 240.0, -35])
    sleep(1)
    robot.movement.joint_to_joint_move([-60.0, 82.0, 240.0, -35])
    sleep(3)

    # Release
    potential_error = robot.dashboard.set_digital_output(1, 0)
    sleep(1)
    robot.dashboard.set_digital_output(2, 1)
    sleep(1)
    potential_error2 = robot.dashboard.set_digital_output(2, 0)
    sleep(1)

    robot.movement.joint_to_joint_move([60.0, 82.0, 240.0, -35])
    sleep(1)
    robot.movement.joint_to_joint_move([60.0, 82.0, 160.0, -35])
    sleep(2)
    robot.movement.joint_to_joint_move([70.0, 82.0, 160.0, -35])
    sleep(2)
    robot.movement.joint_to_joint_move([60.0, 82.0, 160.0, -35])
    sleep(1)
    robot.movement.joint_to_joint_move([30.0, 82.0, 240.0, -35])


    sleep(3)

    """
    robot.movement.joint_to_joint_move([-60.0, 82.0, 240.0, -35])
    sleep(1)
    angle = [-50.0, 82.0, 210.0, -35]
    for i in range (10):
        angle[0] += 12.0
        angle[2] -= 16.0
        robot.movement.joint_to_joint_move(angle)
        sleep(1)
    """
    if potential_error:
        # Handle the errors however you choose!
        print("There was an error turning off the pressure line.")

    if potential_error2:
        # Handle the errors however you choose!
        print("There was an error turning off the vacuum line.")

    robot.dashboard.disable()

    # If you've got this far, you didn't encounter an error or handled it.

    # This API subscribes to the idea that the USER should handle exceptions and not force their hand by raising exceptions, but it comes with the price of simple code being verbose and needing an error handling function.
    # In the future, there will be
    # In the future, there will be