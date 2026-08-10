from dobot_util import Dobot, DobotError
from time import sleep

if __name__ == "__main__":
    ip="192.168.1.6"
    robot = Dobot(ip, logging=True)
    ROBOT_CONNECTED = False


    from dobot_util import Dobot
    print(f"Attempting to connect to robot at {ip}...")
        
    # 1. Establish Network Connection
        
    # 2. Bootup Handshake (Critical for Physical Robot)
    # Clear existing alarms and power on the motors
    print("here2 ")
    robot.dashboard.send_command("ClearError()")
    sleep(0.5)
    print("Here3")
    robot.dashboard.enable()
    print("enabled")
    sleep(5)
        
    ROBOT_CONNECTED = True
    print("Robot connected and enabled successfully!")
    print("trying move")
    robot.movement.joint_to_joint_move([20.0, 10.0, 150.0, -25,0.5])
    print("move order sent")
    robot.dashboard.set_digital_output(1, 0)
    sleep(3)
    print("wait okay hopefuly done")

    potential_error = robot.dashboard.set_digital_output(1, 0)
    sleep(3)
    if potential_error:
        # Handle the errors however you choose!
        print("There was an error turning off the vacuum line.")

    robot.dashboard.disable()

    """
     robot.movement.joint_to_joint_move([-10.0, 82.0, 230.00, -35])
        sleep(1)
        robot.movement.joint_to_joint_move([60.0, 82.0, 70.0, -25])
        sleep(1)


    """
    print("done")
    

    # If you've got this far, you didn't encounter an error or handled it.

    # This API subscribes to the idea that the USER should handle exceptions and not force their hand by raising exceptions, but it comes with the price of simple code being verbose and needing an error handling function.
    # In the future, there will be in-built handler functions on DobotError types to panic or raise exceptions easily, if wanted explicitly.




