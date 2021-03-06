#import threading
#import Queue
import time
import datetime
import pigpio
import ctypes
import os
import math
import numpy as np
import subprocess
import Queue
import RPi.GPIO as GPIO
import threading

from lidar_reader import LidarReader
from servo_handler import ServoHandler
from servo_feedback_reader import ServoFeedbackReader
from graph import Graph
import button_led_handler

# To access Raspberry Pi's GPIO pins:
#import RPi.GPIO as GPIO

class Robot(object):

    def __init__(self):
        # Run the pigpio daemon
        #os.system('sudo pigpiod') # TODO: Make sure this actually works
        #subprocess.call('sudo pigpiod', shell=True)
        
        GPIO.setmode(GPIO.BCM)     # Number GPIOs by channelID
        GPIO.setwarnings(False)    # Ignore Errors
        
        # Setup all GPIO pins as low.
        GPIO.setup(6, GPIO.OUT)     # GPIO_06 = Lidar0
        GPIO.setup(13, GPIO.OUT)     # GPIO_13 = Lidar1
        GPIO.setup(19, GPIO.OUT)     # GPIO_19 = Lidar2
        GPIO.setup(26, GPIO.OUT)     # GPIO_26 = Lidar3
        
        # Turn off all lidars to prepare them to be reset
        #GPIO.output(6, 0)
        #GPIO.output(13, 0)
        #GPIO.output(19, 0)
        #GPIO.output(26, 0)
        
        # Set up queues designated for telling other threads to terminate
        self.lidar_kill_queue = Queue.Queue()
        self.servo_feedback_kill_queue = Queue.Queue()
        self.buttoninput_kill_queue = Queue.Queue()
        
        self.lib = ctypes.cdll.LoadLibrary(os.path.abspath('/home/pi/A-Maze/'
                                                        'libsensordata.so'))

        subprocess.call("python init_i2c.py", shell=True)
        self.lib.init()
        self.pi = pigpio.pi() # binds to port 8888 by default
        # For servo feedback data: set the return types to double
        self.lib.servo_angle_ne.restype = ctypes.c_double
        self.lib.servo_angle_nw.restype = ctypes.c_double
        self.lib.servo_angle_se.restype = ctypes.c_double
        self.lib.servo_angle_sw.restype = ctypes.c_double
        
        # For lidar mm range data: set the return types to int
        self.lib.lidar_distance_north.restype = ctypes.c_int
        self.lib.lidar_distance_south.restype = ctypes.c_int
        self.lib.lidar_distance_east.restype = ctypes.c_int
        self.lib.lidar_distance_west.restype = ctypes.c_int

        self.target_hdg = 0
        
        self.servo_handler = None
        self.initialize_lidars()
        self.initialize_servos()
        self.initialize_reset_button()
        self.lidar_queue = self.lreader.lidar_queue
        self.servo_feedback_queue = self.servo_feedback_reader.servo_feedback_queue
        # At a distance of 60 mm, robot is close to wall
        self.NEAR_WALL_THRESH = 65
        self.TOUCHING_WALL_THRESH = 30
        # NOTE: If the robot is perfectly centered and oriented straight in a
        #       square, then a lidar's distance to a corresponding neighboring
        #       wall is about 45 mm (based on experimental lidar readout)
        # NOTE: Upper bound of servo range is about 100 mm
        self.motion_list = [self.servo_handler.move_north,
                            self.servo_handler.move_south,
                            self.servo_handler.move_east,
                            self.servo_handler.move_west]
        self.robot_radius = 65.0 # mm. Radius from geometric center of robot's
                               # base to center of each omniwheel
        self.omniwheel_radius = 19.0 # mm

    # TODO: theta (heading) should perhaps be accounted for. Currently set to a
    #       default value of 0.
    def move(self, u, v, t, theta=0):
        pass
        
    def initialize_lidars(self):
        self.lreader = LidarReader(self.lib, self.lidar_kill_queue)
        # Call the LidarReader object's start() method, starting its thread
        self.lreader.start()
        print 'LidarReader thread started'
        
    def initialize_servos(self):
        self.servo_handler = ServoHandler(self.lib, self.pi)
        self.servo_feedback_reader = ServoFeedbackReader(self.servo_handler, self.servo_feedback_kill_queue)
        # Start the servo feedback reader's thread
        self.servo_feedback_reader.start()
        print 'Servo feedback thread started'

    def initialize_reset_button(self):
        self.buttoninput = button_led_handler.ButtonInput(self.lib, self.pi, 'black', self.buttoninput_kill_queue)
        self.buttoninput.start()
        print 'Reset button thread started'
        self.button_queue = self.buttoninput.button_queue
        
    def initialize_leds(self):
        self.led = LEDOutput(self.lib, self.pi)
    
    def mainloop(self):
        
        # Manually test rotation of servo
        # while True:
        #     print self.servo_handler.get_angle_position_feedback()[0]
        
        print 'Start of main loop'
        t_elapsed = 0
        t_start = time.time()
        cur_time = t_start
        
        # Get initial angular positions of each servo:
        prev_angles = self.servo_handler.get_angle_position_feedback()
                
        # Initial pose and velocity and time
        self.prev_pose = PoseVelTimestamp(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, t_start)
        
        self.servo_handler.move_north()
        
                
        # # Test approximately 1 revolution of the northeast omniwheel
        # start_angle = self.servo_handler.get_angle_position_feedback()[0]
        # cur_angle = start_angle
        # while True:
        #     cur_angle = self.servo_handler.get_angle_position_feedback()[0]
        #     if cur_angle < start_angle and cur_angle > (start_angle - 0.01):
        #         return

        self.prev_move_dir = None
        
        # Indicates absence of lidar data at any given loop
        lidar_data = None
        # Indicates absence of servo position feedback data at any given loop
        feedback_data = None
        
        # while True:

        self.square_x = 0 # index of maze
        self.square_y = 0 # index of maze
        self.last_square_at_x = 0 # mm
        self.last_square_at_y = 0 # mm
        
        self.found_center = False
        self.time_for_fast_run = False
        self.time_to_shutdown = False

        self.graph = Graph()
        self.in_first_square()
        #while (t_elapsed < 10):
        while True:
            cur_time = time.time() # TODO: @ CONSIDER TESTING

            # TODO: filter heading values to not fluctuate so quickly based on a
            #       single instance of servo position feedback data?
            # while (abs(self.prev_pose.hdg) < math.radians(45)):
            # lidar_data = self.lidar_queue.get(block=True)
            # Try to read Lidar data, if new data has come in
            if not self.lidar_queue.empty():
                lidar_data = self.lidar_queue.get()
                self.check_if_new_square(lidar_data)
                '''
                if (self.servo_handler.direction == ServoHandler.DIRECTION_NORTH and
                    lidar_data.north_dist <= self.NEAR_WALL_THRESH and not lidar_data.is_west_wall()):
                    self.servo_handler.move_west()
                elif (self.servo_handler.direction == ServoHandler.DIRECTION_NORTH and
                      lidar_data.north_dist <= self.NEAR_WALL_THRESH and not lidar_data.is_east_wall()):
                    self.servo_handler.move_east()
                elif (self.servo_handler.direction == ServoHandler.DIRECTION_WEST and
                    lidar_data.west_dist <= self.NEAR_WALL_THRESH and not lidar_data.is_south_wall()):
                    print last_square_at_x - self.cur_pose.x
                    self.servo_handler.move_south()
                elif self.servo_handler.direction == ServoHandler.DIRECTION_SOUTH and lidar_data.south_dist <= self.NEAR_WALL_THRESH:
                    self.servo_handler.move_north()
                elif (self.servo_handler.direction == ServoHandler.DIRECTION_EAST and
                    lidar_data.east_dist <= self.NEAR_WALL_THRESH and not lidar_data.is_south_wall()):
                    self.servo_handler.move_south()
                #print lidar_data.to_string()
                self.adjust_servos_from_lidar(lidar_data)
                '''
                #print '------------------'
               # print 'LIDAR:'
               # print lidar_data.to_string()
                
            # TODO: Work on this angle position feedback getting. Perhaps there
            # is a notable disparity between servos' feedback frequencies, which
            # would probably need to be accounted for in a more sophisticated
            # manner...
            #time.sleep(0.1)
            
            if self.time_to_shutdown:
                break
            
            if not self.servo_feedback_queue.empty():
                feedback_data = self.servo_feedback_queue.get()
                # print '------------------'
                # print 'SERVO FEEDBACK:'
                # print feedback_data.to_string()
                
                angles = [feedback_data.ne_angle,
                          feedback_data.nw_angle,
                          feedback_data.se_angle,
                          feedback_data.sw_angle]
            
                delta_angles = self.servo_handler.get_delta_angles(prev_angles, # TODO: @ CONSIDER TESTING
                                                                    angles)
                for x in delta_angles:
                    if abs(x) <= 0.000001:
                        print "Updating too fast"
                #print 'T DIFF (period): '
                #print time.time() - cur_time
                self.cur_pose = self.compute_cur_pose(delta_angles, cur_time)
                #print self.cur_pose.to_string()

                self.proportional_adjust_servos()

                '''
                if self.servo_handler.direction == ServoHandler.DIRECTION_NORTH and self.cur_pose.y - self.last_square_at_y >= 180:
                    self.in_new_square()
                    self.square_y += 1
                    print "(" + str(self.square_x) + ", " + str(self.square_y) + ")"
                    self.last_square_at_y = self.cur_pose.y
                elif self.servo_handler.direction == ServoHandler.DIRECTION_WEST and self.last_square_at_x - self.cur_pose.x >= 180:
                    self.in_new_square()
                    self.square_x -= 1
                    print "(" + str(self.square_x) + ", " + str(self.square_y) + ")"
                    self.last_square_at_x = self.cur_pose.x
                elif self.servo_handler.direction == ServoHandler.DIRECTION_SOUTH and self.last_square_at_y - self.cur_pose.y >= 180:
                    self.in_new_square()
                    self.square_y -= 1
                    print "(" + str(self.square_x) + ", " + str(self.square_y) + ")"
                    last_square_at_y = self.cur_pose.y
                elif self.servo_handler.direction == ServoHandler.DIRECTION_EAST and self.cur_pose.x - self.last_square_at_x >= 180:
                    self.in_new_square()
                    self.square_x += 1
                    print "(" + str(self.square_x) + ", " + str(self.square_y) + ")"
                    self.last_square_at_x = self.cur_pose.x
                '''

                self.prev_pose = self.cur_pose
               # print self.cur_pose.heading_deg

                prev_angles = angles
                
            lidar_data = None
            feedback_data = None

            # EMERGENCY RESET BUTTON WAS CLICKED
            if not self.button_queue.empty():
                raise ResetException()

            # Exit condition
            t_elapsed = cur_time - t_start
            
            
            
            # TODO: should heading be error-corrected in a PID control loop?
            # Ideally, we want to keep a constant heading and simply translate
            # the robot
                        
            # Make the robot move based on lidar data and servo position
            # feedback
            # TODO: Implement servo position feedback
            # TODO: This is a very naive test of movement based on sensor data
            # if lidar_data is not None:
            #     self.move_to_open_space(lidar_data) # TODO

        self.servo_handler.stop_all()

    def check_if_new_square(self, lidar_data):
        if (self.servo_handler.direction == ServoHandler.DIRECTION_NORTH and
            ((lidar_data.is_north_wall() and lidar_data.north_dist <= self.NEAR_WALL_THRESH) or
                self.cur_pose.y - self.last_square_at_y >= 180)):
                self.last_square_at_y = self.cur_pose.y
                self.square_y += 1
                self.in_new_square(lidar_data)
                self.decide_turn()
                print "(" + str(self.square_x) + ", " + str(self.square_y) + ")"
        if (self.servo_handler.direction == ServoHandler.DIRECTION_WEST and
            ((lidar_data.is_west_wall() and lidar_data.west_dist <= self.NEAR_WALL_THRESH) or
                self.last_square_at_x - self.cur_pose.x >= 180)):
                self.last_square_at_x = self.cur_pose.x
                self.square_x -= 1
                self.in_new_square(lidar_data)
                self.decide_turn()
                print "(" + str(self.square_x) + ", " + str(self.square_y) + ")"
        if (self.servo_handler.direction == ServoHandler.DIRECTION_SOUTH and
            ((lidar_data.is_south_wall() and lidar_data.south_dist <= self.NEAR_WALL_THRESH) or
                self.last_square_at_y - self.cur_pose.y >= 180)):
                self.last_square_at_y = self.cur_pose.y
                self.square_y -= 1
                self.in_new_square(lidar_data)
                self.decide_turn()
                print "(" + str(self.square_x) + ", " + str(self.square_y) + ")"
        if (self.servo_handler.direction == ServoHandler.DIRECTION_EAST and
            ((lidar_data.is_east_wall() and lidar_data.east_dist <= self.NEAR_WALL_THRESH) or
                self.cur_pose.x - self.last_square_at_x >= 180)):
                self.last_square_at_x = self.cur_pose.x
                self.square_x += 1
                self.in_new_square(lidar_data)
                self.decide_turn()
                print "(" + str(self.square_x) + ", " + str(self.square_y) + ")"

    def in_first_square(self):
        self.graph.updateNodeVisit()
        self.graph.addEdge(0, 0, 0, 1)

    def in_new_square(self, lidar_data):
        self.graph.setCurrentNode(self.square_x, self.square_y)
        self.graph.updateNodeVisit()
        if not lidar_data.is_north_wall():
            self.graph.addEdge(self.square_x, self.square_y, self.square_x, self.square_y + 1)
        if not lidar_data.is_west_wall():
            self.graph.addEdge(self.square_x, self.square_y, self.square_x - 1, self.square_y)
        if not lidar_data.is_south_wall():
            self.graph.addEdge(self.square_x, self.square_y, self.square_x, self.square_y - 1)
        if not lidar_data.is_east_wall():
            self.graph.addEdge(self.square_x, self.square_y, self.square_x + 1, self.square_y)
        
        if not self.found_center:
            if self.check_if_win():
                self.found_center = True
                print 'Maze has been solved; robot is in center of the maze!'
                self.win_node_x, self.win_node_y = self.graph.getCurrentNode().getXY()
                self.shortest_path, self.shortest_path_back = self.graph.shortestPath(0, 0, self.win_node_x, self.win_node_y)
                print 'SP'
                for node in self.shortest_path:
                    print node.getXY()
            # Want to run the shortest path back and then run the shortest path once we are back at the origin node
            
        
    def check_if_win(self):
        #option1
        # if self.square_x <= 14 and self.square_y <= 14:
        #     return (self.graph.isConnected(self.square_x,self.square_y, self.square_x + 1, self.square_y)
        #       and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y + 1)
        #       and self.graph.isConnected(self.square_x + 1, self.square_y, self.square_x + 1, self.square_y + 1)
        #       and self.graph.isConnected(self.square_x, self.square_y+1, self.square_x + 1, self.square_y + 1))
        #
        # #option2
        # if self.square_x >= 1 and self.square_y <= 14:
        #     return (self.graph.isConnected(self.square_x, self.square_y, self.square_x - 1, self.square_y)
        #       and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y + 1)
        #       and self.graph.isConnected(self.square_x, self.square_y + 1, self.square_x - 1, self.square_y + 1)
        #       and self.graph.isConnected(self.square_x - 1, self.square_y, self.square_x - 1, self.square_y + 1))
        #
        # #option3
        # if self.square_x >= 1 and self.square_y >= 1:
        #     return (self.graph.isConnected(self.square_x, self.square_y, self.square_x - 1, self.square_y)
        #       and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y - 1)
        #       and self.graph.isConnected(self.square_x, self.square_y - 1, self.square_x - 1, self.square_y)
        #       and self.graph.isConnected(self.square_x - 1, self.square_y, self.square_x, self.square_y - 1))
        #
        # #option4
        # if self.square_x <= 14 and self.square_y >= 1:
        #     return (self.graph.isConnected(self.square_x, self.square_y, self.square_x + 1, self.square_y)
        #       and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y - 1)
        #       and self.graph.isConnected(self.square_x + 1, self.square_y, self.square_x + 1, self.square_y - 1)
        #       and self.graph.isConnected(self.square_x, self.square_y - 1, self.square_x + 1, self.square_y - 1))
        
        cases_to_check = [False, False, False, False]
        
        if self.square_x <= 14 and self.square_y <= 14:
            cases_to_check[0] = True
            
        if self.square_x >= 1 and self.square_y <= 14:
            cases_to_check[1] = True
            
        if self.square_x >= 1 and self.square_y >= 1:
            cases_to_check[2] = True
            
        if self.square_x <= 14 and self.square_y >= 1:
            cases_to_check[3] = True
            
        cases = [self.case1, self.case2, self.case3, self.case4]
        
        for num, checking in enumerate(cases_to_check):
            if checking:
                if cases[num]():
                    return True
        
        # print 'x:',self.square_x
        # print 'y:',self.square_y
        # return ((self.graph.isConnected(self.square_x, self.square_y, self.square_x + 1, self.square_y)
        #       and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y + 1)
        #       and self.graph.isConnected(self.square_x + 1, self.square_y, self.square_x + 1, self.square_y + 1)
        #       and self.graph.isConnected(self.square_x, self.square_y+1, self.square_x + 1, self.square_y + 1))
        #       or
        #       (self.graph.isConnected(self.square_x, self.square_y, self.square_x - 1, self.square_y)
        #         and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y + 1)
        #         and self.graph.isConnected(self.square_x, self.square_y + 1, self.square_x - 1, self.square_y + 1)
        #         and self.graph.isConnected(self.square_x - 1, self.square_y, self.square_x - 1, self.square_y + 1))
        #       or
        #       (self.graph.isConnected(self.square_x, self.square_y, self.square_x - 1, self.square_y)
        #         and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y - 1)
        #         and self.graph.isConnected(self.square_x, self.square_y - 1, self.square_x - 1, self.square_y)
        #         and self.graph.isConnected(self.square_x - 1, self.square_y, self.square_x, self.square_y - 1))
        #       or
        #       (self.graph.isConnected(self.square_x, self.square_y, self.square_x + 1, self.square_y)
        #         and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y - 1)
        #         and self.graph.isConnected(self.square_x + 1, self.square_y, self.square_x + 1, self.square_y - 1)
        #         and self.graph.isConnected(self.square_x, self.square_y - 1, self.square_x + 1, self.square_y - 1))
        #       )
              
    def case1(self):
        return (self.graph.isConnected(self.square_x, self.square_y, self.square_x + 1, self.square_y)
              and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y + 1)
              and self.graph.isConnected(self.square_x + 1, self.square_y, self.square_x + 1, self.square_y + 1)
              and self.graph.isConnected(self.square_x, self.square_y+1, self.square_x + 1, self.square_y + 1))
    
    def case2(self):
        return (self.graph.isConnected(self.square_x, self.square_y, self.square_x - 1, self.square_y)
          and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y + 1)
          and self.graph.isConnected(self.square_x, self.square_y + 1, self.square_x - 1, self.square_y + 1)
          and self.graph.isConnected(self.square_x - 1, self.square_y, self.square_x - 1, self.square_y + 1))
          
    def case3(self):
        return (self.graph.isConnected(self.square_x, self.square_y, self.square_x - 1, self.square_y)
          and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y - 1)
          and self.graph.isConnected(self.square_x, self.square_y - 1, self.square_x - 1, self.square_y)
          and self.graph.isConnected(self.square_x - 1, self.square_y, self.square_x, self.square_y - 1))
          
    def case4(self):
        return (self.graph.isConnected(self.square_x, self.square_y, self.square_x + 1, self.square_y)
          and self.graph.isConnected(self.square_x, self.square_y, self.square_x, self.square_y - 1)
          and self.graph.isConnected(self.square_x + 1, self.square_y, self.square_x + 1, self.square_y - 1)
          and self.graph.isConnected(self.square_x, self.square_y - 1, self.square_x + 1, self.square_y - 1))

    def decide_turn(self):
        adjacent_nodes = self.graph.findAdjacent(self.graph.getCurrentNode())
        turn_node = min(adjacent_nodes, key = lambda x: x.getVisited())
        node_x, node_y = turn_node.getXY()
        
        if self.found_center and not self.time_for_fast_run:
            cur_node_x, cur_node_y = self.shortest_path_back[0].getXY()
            if cur_node_x == 0 and cur_node_y == 0:
                self.time_for_fast_run = True
                self.fast_run_enabled = False
                self.servo_handler.stop_all()
            else:
                # Next node values. Replaces those computed in the above findAdjacent code
                node_x, node_y = self.shortest_path_back[1].getXY()
                self.shortest_path_back.pop(0) # remove first element from list
                
        if self.time_for_fast_run:
            if not self.fast_run_enabled:
                if self.pi.wait_for_edge(23, pigpio.FALLING_EDGE,10800):
                    self.fast_run_enabled = True
                    cur_node_x, cur_node_y = self.shortest_path[0].getXY()
                    if cur_node_x == self.win_node_x and cur_node_y == self.win_node_y:
                        # At this point, we are done! Shutdown the robot
                        self.shutdown()
                    else:
                        # Next node values. Replaces those computed in the above findAdjacent code
                        node_x, node_y = self.shortest_path[1].getXY()
                        self.shortest_path.pop(0) # remove first element from list
            else:
                cur_node_x, cur_node_y = self.shortest_path[0].getXY()
                if cur_node_x == self.win_node_x and cur_node_y == self.win_node_y:
                    # At this point, we are done! Shutdown the robot
                    self.shutdown()
                    return
                else:
                    # Next node values. Replaces those computed in the above findAdjacent code
                    node_x, node_y = self.shortest_path[1].getXY()
                    self.shortest_path.pop(0) # remove first element from list
                
            
        if node_x - self.square_x == 1:
            self.servo_handler.move_east() # TODO: WE SHOULD NOT BE CALLING THESE FROM LIDAR DATA IN ROBOT.PY, SHOULD WE? WE SHOULD USE CORRECTED VALUES FROM P-ONLY LOOP, NO? NOT SURE... TALK THROUGH THIS WITH NOAH
        elif self.square_x - node_x == 1:
            self.servo_handler.move_west()
        elif node_y - self.square_y == 1:
            self.servo_handler.move_north()
        elif self.square_y - node_y == 1:
            self.servo_handler.move_south()

    def proportional_adjust_servos(self):
        K_p = 4
        dir = self.servo_handler.direction
        if dir == ServoHandler.DIRECTION_NORTH:
            self.servo_handler.adjust_signal("sw", (self.cur_pose.hdg - self.target_hdg) * K_p)
        elif dir == ServoHandler.DIRECTION_WEST:
            self.servo_handler.adjust_signal("se", (self.cur_pose.hdg - self.target_hdg) * K_p)
        elif dir == ServoHandler.DIRECTION_SOUTH:
            self.servo_handler.adjust_signal("ne", (self.cur_pose.hdg - self.target_hdg) * K_p)
        elif dir == ServoHandler.DIRECTION_EAST:
            self.servo_handler.adjust_signal("nw", (self.cur_pose.hdg - self.target_hdg) * K_p)

    def adjust_servos_from_lidar(self, lidar_data):
        '''
        Execute wall avoidance based on lidar data
        '''
        K_p = 10
        dir = self.servo_handler.direction
        
        '''
        if lidar_data.too_close_to_north():
            self.servo_handler.move_south()
            self.reset_dir = dir
        if lidar_data.too_close_to_west():
            self.servo_handler.move_east()
            self.reset_dir = dir
        if lidar_data.too_close_to_south():
            self.servo_handler.move_north()
            self.reset_dir = dir
        if lidar_data.too_close_to_east():
            self.servo_handler.move_west()
            self.reset_dir = dir
        '''
        
        if lidar_data.is_south_wall() and lidar_data.is_north_wall():
            if (lidar_data.north_dist <= 40/((math.cos(self.cur_pose.hdg))**2)):
                dir_sign = 1
                if (self.servo_handler.direction == ServoHandler.DIRECTION_EAST):
                    dir_sign = -1
                elif (self.servo_handler.direction == ServoHandler.DIRECTION_WEST):
                    dir_sign = 1
                self.target_hdg = dir_sign * K_p * (1/lidar_data.north_dist)
            elif (lidar_data.south_dist <= 40/((math.cos(self.cur_pose.hdg))**2)):
                dir_sign = 1
                if (self.servo_handler.direction == ServoHandler.DIRECTION_EAST):
                    dir_sign = 1
                elif (self.servo_handler.direction == ServoHandler.DIRECTION_WEST):
                    dir_sign = -1
                self.target_hdg = dir_sign * K_p * (1/lidar_data.south_dist)
            elif abs((lidar_data.south_dist - lidar_data.north_dist)/math.cos(self.cur_pose.hdg)) <= 5:
                # Wall-avoidance correction brought the robot more toward the center, so readjust target heading
                self.target_hdg = 0
        elif lidar_data.is_east_wall() and lidar_data.is_west_wall():
            if (lidar_data.west_dist <= 40/((math.cos(self.cur_pose.hdg))**2)):
                # Which direction to set the target heading depends on direction of motion
                dir_sign = 1
                if (self.servo_handler.direction == ServoHandler.DIRECTION_NORTH):
                    dir_sign = -1
                elif (self.servo_handler.direction == ServoHandler.DIRECTION_SOUTH):
                    dir_sign = 1
                self.target_hdg = dir_sign * K_p * (1/lidar_data.west_dist)
            elif (lidar_data.east_dist <= 40/((math.cos(self.cur_pose.hdg))**2)):
                dir_sign = 1
                if (self.servo_handler.direction == ServoHandler.DIRECTION_NORTH):
                    dir_sign = 1
                elif (self.servo_handler.direction == ServoHandler.DIRECTION_SOUTH):
                    dir_sign = -1
                self.target_hdg = dir_sign * K_p * (1/lidar_data.east_dist)
            elif abs((lidar_data.east_dist - lidar_data.west_dist)/math.cos(self.cur_pose.hdg)) <= 5:
                self.target_hdg = 0

        
        '''
        if ((dir == ServoHandler.DIRECTION_NORTH or dir == ServoHandler.DIRECTION_SOUTH) and
            lidar_data.is_west_wall() and lidar_data.is_east_wall()):
            off_center = lidar_data.west_dist - lidar_data.east_dist
            if dir == ServoHandler.DIRECTION_SOUTH:
                K_p = -K_p
            print off_center
            if off_center > 20: # too far to east
                self.servo_handler.adjust_signal("ne", K_p * -off_center)
                print "Too far to east, turning to left"
           #     noservocorrect = True
            elif off_center < -20:  # too far to west
                self.servo_handler.adjust_signal("nw", K_p * off_center)
           #     noservocorrect = True
           # else:
           #     noservocorrect = False
        '''

    def compute_cur_pose(self, delta_angles, t):
        '''
        Input:
            - delta_angles: a list of 4 angle measurements (angle measured from
                            0 to 1) that represent are the change in angular
                            position of each servo as a result of rotation
            - t: current timestamp (seconds since the epoch)
        Output:
            - the current pose after applying the rotation and translation
              given by the servo position feedback data
               - or None if angular velocities do not pass the filter
        
        Steps to compute current pose:
            1. Compute average angular velocity of each omniwheel over the time
               interval delta_t
            2. Use the derived rigid-body equations that describe this system.
               These solution equations include three equations of interest:
                    1) V_er = self.omniwheel_radius/math.sqrt(2) * (w_A - w_B),
                        where V_er is the component of the robot's velocity in
                        the e_r (forward) direction (local robot coordinates),
                        and where w_A, for example, is the angular velocity of
                        omniwheel A (northeast omniwheel)
                    2) V_eT = self.omniwheel_radius/math.sqrt(2) * (w_C - w_B),
                              where V_eT is the component
                              of the robot's velocity in the e_T (sideways)
                              direction (local robot coordinates)
                    3) w_v = (-self.omniwheel_radius*(w_A + w_C))/(2*self.robot_radius),
                        where w_v is the angular velocity of the robot
            3. Multiply w_v by delta_t to obtain an estimate of the change in
               heading of the robot over the time interval delta_t
            4. Use a rotation matrix/tensor with theta equal to previous heading
               + the change in heading over duration of delta_t. Multiply the
               rotation matrix with the {V_er; V_eT} vector to obtain average
               velocity in global frame over time interval delta_t.
                    - Use a linear interpolation of delta_theta (change in
                      heading) to estimate average heading as prev_heading +
                      (delta_theta/2). This is with w_v approximated as constant
                      over delta_t
            5. Multiply average velocity in global frame by delta_t to get
               displacement in global frame
        
        Eventually can also incorporate lidar data to correct these poses,
        perhaps. It is likely that this pose calculation will be prone to
        accumulation/estimation errors.
        '''

        # Change in time between two pose calculations
        delta_t = t - self.prev_pose.t
        #print 'Delta t: ' + str(delta_t)
        
        # 1. Omniwheel angular velocities (signed magnitudes indicating rotation
        #    direction about k axis)
        angular_vels = self.compute_omniwheel_angular_vels(delta_angles,
                                                           delta_t)
        #print angular_vels
        
        # (Pretend the w's are omegas for angular velocity. Negate these to
        # align with CCW=positive mathematical convention)
        w_A = -angular_vels[0] # average angular velocity of NE omniwheel
        w_B = -angular_vels[1] # average angular velocity of NW omniwheel
        w_C = -angular_vels[3] # average angular velocity of SW omniwheel
        w_D = -angular_vels[2] # average angular velocity of SE omniwheel
        # Note that fourth servo (e.g., SE, the one labeled D) makes rigid-body
        # system overconstrained.
        
        #angular_vels_alphabetical = [w_A, w_B, w_C, w_D]
        #prev_angular_vels = [self.prev_pose.w_A, self.prev_pose.w_B, self.prev_pose.w_C, self.prev_pose.w_D]
        # TODO: Consider implementing a more robust filter such as a Kalman
        #       filter
        # omegas_passed = self.filter_omegas(prev_angular_vels, angular_vels_alphabetical)
        # if not omegas_passed:
        #     return None
                                                           
        # 2. Using derived rigid-body equations
        # Velocities components of the robot in local coordinates
        #V_er = self.omniwheel_radius/math.sqrt(2) * (w_A - w_B)
        R = self.omniwheel_radius
        V_er = (math.sqrt(2)*R*w_A)/4 - (math.sqrt(2)*R*w_B)/4 - (math.sqrt(2)*R*w_C)/4 + (math.sqrt(2)*R*w_D)/4
        #V_eT = self.omniwheel_radius/math.sqrt(2) * (w_A/2 - w_B + w_C/2)
        #V_eT = self.omniwheel_radius/math.sqrt(2) * (w_C - w_B)
        V_eT = (math.sqrt(2)*R*w_C)/4 - (math.sqrt(2)*R*w_B)/4 - (math.sqrt(2)*R*w_A)/4 + (math.sqrt(2)*R*w_D)/4
        
        # Angular velocity of entire robot
        # w_v = ((self.omniwheel_radius/math.sqrt(2) * (-w_A/2 - w_C/2))/
        #             self.robot_radius)
        #w_v = (-self.omniwheel_radius*(w_A + w_C))/(2*self.robot_radius)
        R_v = self.robot_radius
        # Does this following equation just do an average like we originally had
        # assumed by intuition / simple analysis? Maybe with extra factors?
        w_v = -(R*w_A)/(4*R_v) - (R*w_B)/(4*R_v) - (R*w_C)/(4*R_v) - (R*w_D)/(4*R_v)
                
        # 3. Estimate of the change in heading over time interval delta_t
        #print '###### w_v: ' + str(w_v)
        robot_delta_hdg = w_v * delta_t
        
        # 4. Convert from local to global coordinates with a rotation matrix
        # Average heading estimate
        #robot_average_hdg = self.prev_pose.hdg + (robot_delta_hdg/2)
        robot_average_hdg = self.prev_pose.hdg + robot_delta_hdg
        
        # Apply filter to raw heading value (robot_average_hdg)
        # TODO

        # TODO: Do this coordinate frame transformation correctly...
        average_vel_robot_frame = np.matrix(
                        [[V_eT], # like i component in local coords.
                         [V_er]]) # like j component in local coords.
        #print average_vel_robot_frame
                                                     
        # Convert from local robot coordinates to world coordinates by
        # multiplying a rotation matrix by the robot's velocity in the local
        # frame
        # TODO: Do this coordinate frame transformation correctly...
        average_vel_world_frame = self.apply_rotation_matrix(
                                            average_vel_robot_frame,
                                            robot_average_hdg).tolist()
        # 5. Multiply average velocity in global frame by delta_t to get
        #    displacement in global frame
        # Displacement in global frame (s = v_(avg) * t)
        displacement_world_frame = [average_vel_world_frame[0][0] * delta_t,
                                    average_vel_world_frame[1][0] * delta_t]
        return PoseVelTimestamp(self.prev_pose.x - displacement_world_frame[0],
                                self.prev_pose.y - displacement_world_frame[1],
                                -average_vel_world_frame[0][0],
                                -average_vel_world_frame[1][0],
                                robot_average_hdg,
                                w_v,
                                w_A,
                                w_B,
                                w_C,
                                w_D,
                                t)
                                
    def filter_omegas(self, prev_angular_vels, cur_anguler_vels):
        for i in range(len(cur_anguler_vels)):
            if abs(cur_anguler_vels[i] - prev_angular_vels[i]) > 2:
                return False
        return True

    def compute_omniwheel_angular_vels(self, delta_angles, delta_t):
        angular_vels = []
        for angle in delta_angles:
            theta = (2 * math.pi * angle)
            # Omega is rate of change of theta. Get an average value by dividing
            # by delta_t
            omega = theta/delta_t
            # Note that clockwise rotation of an omniwheel corresponds to a
            # positive value of omega
            angular_vels.append(omega)
        return angular_vels
        
    def apply_rotation_matrix(self, original_matrix, hdg):
        rotation_mat = np.matrix([[math.cos(hdg), -math.sin(hdg)],
                                  [math.sin(hdg), math.cos(hdg)]])
        return rotation_mat*original_matrix
            
    # TODO: Use a better algorithm for movement. This is just for basic testing
    # of responsiveness to lidar data
    def move_to_open_space(self, lidar_data):
        data = [lidar_data.north_dist, lidar_data.south_dist,
                lidar_data.east_dist, lidar_data.west_dist]
        room_to_move = []
        num_move_options = 0
        for dist in data:
            if dist > self.NEAR_WALL_THRESH:
                room_to_move.append(True)
                num_move_options += 1
            else:
                room_to_move.append(False)
                
        if self.prev_move_dir is None:
            # Set initial motion
            self.prev_move_dir = room_to_move.index(True)
            self.move_by_lidar_wall_avoidance(self.prev_move_dir)
            return

        if num_move_options == 1:
            self.move_dir = room_to_move.index(True)
        elif num_move_options > 1:
            # There are multiple feasible directions
            # If the current direction is not a feasible direction, then go in
            # the first free one.
            if not room_to_move[self.prev_move_dir]:
                # Go in the first free one
                self.move_dir = room_to_move.index(True)
                
        if self.prev_move_dir != self.move_dir:
            # Only send a servo signal if the movement directions are different
            self.move_by_lidar_wall_avoidance(self.move_dir)
        self.prev_move_dir = self.move_dir

            
    def move_by_lidar_wall_avoidance(self, direction):
        # Based on lidar closeness
        # direction is an integer indicating which of the 4 cardinal directions
        # to move in. 0 = north, 1 = south, 2 = east, 3 = west
        self.motion_list[direction]()
        
    #def is_about_to_hit_wall(self)
        
    def shutdown(self):
        # TODO: Add things to this?
        self.time_to_shutdown = True
        self.lidar_kill_queue.put(True)
        self.servo_feedback_kill_queue.put(True)
        self.buttoninput_kill_queue.put(True)
        if self.servo_handler is not None:
            self.servo_handler.close_handler()
        # Allow time for threads of previous robot to get messages put on the
        # kill queues and quit
        time.sleep(5)
            

# (Really just a vehicle state class...)
# Class that represents the robot's pose in the global/world coordinate frame:
#   - x-coordinate (mm) in global frame
#   - y-coordinate (mm) in global frame
#   - u: velocity in x direction (mm/s) in global frame
#   - v: velocity in y direction (mm/s) in global frame
#   - heading (radians) in global frame
#   - w_v: angular velocity of robot (rad/s)
#   - w_A = average angular velocity of NE omniwheel
#   - w_B = average angular velocity of NW omniwheel
#   - w_C = average angular velocity of SW omniwheel
#   - w_D = average angular velocity of SE omniwheel
class PoseVelTimestamp(object):
    def __init__(self, x, y, u, v, hdg, w_v, w_A, w_B, w_C, w_D, t):
        self.x = x
        self.y = y
        self.u = u
        self.v = v
        self.hdg = hdg
        self.w_v = w_v
        self.w_A = w_A
        self.w_B = w_B
        self.w_C = w_C
        self.w_D = w_D
        self.t = t
        
        self.heading_deg = math.degrees(self.hdg)
        self.w_v_deg = math.degrees(self.w_v)
        self.w_A_deg = math.degrees(self.w_A)
        self.w_B_deg = math.degrees(self.w_B)
        self.w_C_deg = math.degrees(self.w_C)
        self.w_D_deg = math.degrees(self.w_D)

        
    def to_string(self):
        return_str = '----------\n'
        return_str += 'x: {0}\n'
        return_str += 'y: {1}\n'
        return_str += 'u: {2}\n'
        return_str += 'v: {3}\n'
        return_str += 'heading (deg): {4}\n'
        return_str += 'w_v (deg/s): {5}\n'
        return_str += 'w_A (deg/s): {6}\n'
        return_str += 'w_B (deg/s): {7}\n'
        return_str += 'w_C (deg/s): {8}\n'
        return_str += 'w_D (deg/s): {9}\n'
        return_str += 't (sec): {10}\n'
        return_str += '----------'
        return (return_str.format(self.x, self.y,
                                  self.u, self.v,
                                  self.heading_deg, self.w_v_deg,
                                  self.w_A_deg, self.w_B_deg,
                                  self.w_C_deg, self.w_D_deg,
                                  self.t))
                
class ResetException(Exception):
    pass

if __name__ == '__main__':
    robot = None
    num_loops = 0
    # subprocess.call('sudo pigpiod', shell=True)
    try:
        while True:
            num_loops += 1
            print 'NUM_LOOPS:', num_loops
            # Seems that each pigpio.pi() opens a new thread
            # (see output of threading.active_count()). This takes up RPi
            # resources and affects its driving capabilities, it seems, so be
            # sure to close the temp_pi_handler each loop...
            temp_pi_handler = pigpio.pi() # Temp handler to listen to start button
            print '################## NUM THREADS:',threading.active_count()
            print "Waiting for start press..."
            if temp_pi_handler.wait_for_edge(23, pigpio.FALLING_EDGE,10800):
                print 'White button pressed, robot class starting'
                robot = Robot()

                try:
                    robot.mainloop()
                except ResetException:
                    print 'EMERGENCY! STOPPING!...'
                    robot.shutdown()
                    print 'Press White Button to start again'
                    temp_pi_handler.stop()
                    # Manually close threads. They are daemon threads, but since main thread does not end,
                    continue
                except KeyboardInterrupt as e:
                    print e
                    break
                    temp_pi_handler.stop()
                # except Exception as e:
                #     print '!!!!!!!!', e
                #     temp_pi_handler.stop()
            else:
                break
    finally:
        print 'Terminating robot.py program'
        GPIO.cleanup()
        # subprocess.call('sudo killall pigpiod', shell=True)
        if robot is not None:
            robot.shutdown()
        print 'FINALLY'
