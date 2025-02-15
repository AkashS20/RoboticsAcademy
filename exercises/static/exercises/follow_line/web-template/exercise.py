#!/usr/bin/env python
import logging

from websocket_server import WebsocketServer
import time
import threading
import multiprocessing
import subprocess
import sys
from datetime import datetime
import re
import json
import importlib

import rospy
from std_srvs.srv import Empty
import cv2

from shared.value import SharedValue

from hal import HAL
from brain import BrainProcess

from teleoperator import TeleopThread
import queue


class Template:
    # Initialize class variables
    # self.time_cycle to run an execution for atleast 1 second
    # self.process for the current running process
    def __init__(self):
        self.brain_process = None
        self.reload = multiprocessing.Event()

        # Time variables
        self.brain_time_cycle = SharedValue('brain_time_cycle')
        self.brain_ideal_cycle = SharedValue('brain_ideal_cycle')
        self.real_time_factor = 0

        self.frequency_message = {'brain': '', 'gui': '', 'rtf': ''}

        # GUI variables
        self.gui_time_cycle = SharedValue('gui_time_cycle')
        self.gui_ideal_cycle = SharedValue('gui_ideal_cycle')

        self.server = None
        self.client = None
        self.host = sys.argv[1]

        # Initialize the GUI and HAL behind the scenes
        self.hal = HAL()

        self.exit_signal_teleop = threading.Event()
        self.teleop_q = queue.Queue()
        self.teleop = TeleopThread(self.teleop_q,self.exit_signal_teleop,self.hal)
        self.paused = False

    # Function for saving
    def save_code(self, source_code):
        with open('code/academy.py', 'w') as code_file:
            code_file.write(source_code)

    # Function for loading
    def load_code(self):
        with open('code/academy.py', 'r') as code_file:
            source_code = code_file.read()

        return source_code

    # Function to parse the code
    # A few assumptions:
    # 1. The user always passes sequential and iterative codes
    # 2. Only a single infinite loop
    def parse_code(self, source_code):
        # Check for save/load
        if(source_code[:5] == "#save"):
            source_code = source_code[5:]
            self.save_code(source_code)

            return "", ""

        elif(source_code[:5] == "#load"):
            source_code = source_code + self.load_code()
            self.server.send_message(self.client, source_code)

            return "", ""        

        else:
            sequential_code, iterative_code = self.seperate_seq_iter(source_code[6:])
            return iterative_code, sequential_code

    # Function to seperate the iterative and sequential code
    def seperate_seq_iter(self, source_code):
        if source_code == "":
            return "", ""

        # Search for an instance of while True
        infinite_loop = re.search(r'[^ ]while\s*\(\s*True\s*\)\s*:|[^ ]while\s*True\s*:|[^ ]while\s*1\s*:|[^ ]while\s*\(\s*1\s*\)\s*:', source_code)

        # Seperate the content inside while True and the other
        # (Seperating the sequential and iterative part!)
        try:
            start_index = infinite_loop.start()
            iterative_code = source_code[start_index:]
            sequential_code = source_code[:start_index]

            # Remove while True: syntax from the code
            # And remove the the 4 spaces indentation before each command
            iterative_code = re.sub(r'[^ ]while\s*\(\s*True\s*\)\s*:|[^ ]while\s*True\s*:|[^ ]while\s*1\s*:|[^ ]while\s*\(\s*1\s*\)\s*:', '', iterative_code)
            # Add newlines to match line on bug report
            extra_lines = sequential_code.count('\n')
            while (extra_lines >= 0):
                iterative_code = '\n' + iterative_code
                extra_lines -= 1
            iterative_code = re.sub(r'^[ ]{4}', '', iterative_code, flags=re.M)

        except:
            sequential_code = source_code
            iterative_code = ""

        return sequential_code, iterative_code

    # Function to maintain thread execution
    def execute_thread(self, source_code):
        # Keep checking until the thread is alive
        # The thread will die when the coming iteration reads the flag
        if(self.brain_process != None):
            while self.brain_process.is_alive():
                pass

        # Turn the flag down, the iteration has successfully stopped!
        self.reload.clear()
        # New thread execution
        code = self.parse_code(source_code)
        logging.debug("appy")
        logging.debug(code)
        if code[0] == "" and code[1] == "":
            return

        self.brain_process = BrainProcess(code, self.reload)
        self.brain_process.start()
        self.send_code_message()

    # Function to read and set frequency from incoming message
    def read_frequency_message(self, message):
        frequency_message = json.loads(message)

        # Set brain frequency
        frequency = float(frequency_message["brain"])
        self.brain_time_cycle.add(1000.0 / frequency)

        # Set gui frequency
        frequency = float(frequency_message["gui"])
        self.gui_time_cycle.add(1000.0 / frequency)

        return

    # Function to track the real time factor from Gazebo statistics
    # https://stackoverflow.com/a/17698359
    # (For reference, Python3 solution specified in the same answer)
    def track_stats(self):
        args = ["gz", "stats", "-p"]
        # Prints gz statistics. "-p": Output comma-separated values containing-
        # real-time factor (percent), simtime (sec), realtime (sec), paused (T or F)
        stats_process = subprocess.Popen(args, stdout=subprocess.PIPE, bufsize=0)
        # bufsize=1 enables line-bufferred mode (the input buffer is flushed
        # automatically on newlines if you would write to process.stdin )
        with stats_process.stdout:
            for line in iter(stats_process.stdout.readline, b''):
                stats_list = [x.strip() for x in line.split(b',')]
                self.real_time_factor = stats_list[0].decode("utf-8")

    # Function to generate and send frequency messages

    def send_frequency_message(self):
        # This function generates and sends frequency measures of the brain and gui
        brain_frequency = 0
        gui_frequency = 0
        try:
            brain_frequency = round(1000 / self.brain_ideal_cycle.get(), 1)
        except ZeroDivisionError:
            brain_frequency = 0

        try:
            gui_frequency = round(1000 / self.gui_ideal_cycle.get(), 1)
        except ZeroDivisionError:
            gui_frequency = 0

        self.frequency_message["brain"] = brain_frequency
        self.frequency_message["gui"] = gui_frequency
        self.frequency_message["rtf"] = self.real_time_factor

        message = "#freq" + json.dumps(self.frequency_message)
        self.server.send_message(self.client, message)

    def send_ping_message(self):
        self.server.send_message(self.client, "#ping")

    # Function to notify the front end that the code was received and sent to execution
    def send_code_message(self):
        self.server.send_message(self.client, "#exec")

    def read_teleop_message(self, teleop_message):
        teleop_message = json.loads(teleop_message)

        # Get V and W
        v = float(teleop_message["v"])
        w = float(teleop_message["w"])

        return v, w

    # The websocket function
    # Gets called when there is an incoming message from the client
    def handle(self, client, server, message):
        if(message[:5] == "#freq"):
            frequency_message = message[5:]
            self.read_frequency_message(frequency_message)
            time.sleep(1)
            self.send_frequency_message()
            return
        elif(message[:5] == "#ping"):
            time.sleep(1)
            self.send_ping_message()
            return
        elif(message[:5] == "#tele"):
            # Stop Brain code by sending an empty code
            if not self.paused:
                self.reload.set()
                self.execute_thread("""from GUI import GUI
from HAL import HAL
# Enter sequential code!

while True:
    # Enter iterative code!""")
                self.paused = True
                
            # Parse message
            teleop_message = message[5:]
            v,w = self.read_teleop_message(teleop_message)
            
            # crear hebra de interacciones periódicas
            # python thread
            # Clear exit flag in order to continue executing the thread            
            # envío última V y W recibida y me pongo a dormir
            
            # Recupero el flag
            self.exit_signal_teleop.clear()
            
            if not self.teleop.is_alive():
                self.teleop.start()
            
            self.teleop_q.put({"v":v,"w":w})
            return

        elif (message[:5] == "#code"):  
            try:
                # First pause the teleoperator thread if exists
                if self.teleop.is_alive():
                    self.exit_signal_teleop.set()
                
                self.paused = False

                # Once received turn the reload flag up and send it to execute_thread function
                code = message
                # print(repr(code))
                self.reload.set()
                self.execute_thread(code)
            except:
                pass

        elif (message[:5] == "#stop"):
            try:
                self.reload.set()
                self.execute_thread("""from GUI import GUI
from HAL import HAL
# Enter sequential code!

while True:
    # Enter iterative code!""")
                self.paused = True
            except:
                pass
            self.server.send_message(self.client, "#stpd")

    # Function that gets called when the server is connected
    def connected(self, client, server):
        self.client = client
        # Start the HAL update thread
        self.hal.start_thread()

        # Start real time factor tracker thread
        self.stats_thread = threading.Thread(target=self.track_stats)
        self.stats_thread.start()

        # Initialize the ping message
        self.send_frequency_message()

        print(client, 'connected')

    # Function that gets called when the connected closes
    def handle_close(self, client, server):
        print(client, 'closed')

    def run_server(self):
        self.server = WebsocketServer(port=1905, host=self.host)
        self.server.set_fn_new_client(self.connected)
        self.server.set_fn_client_left(self.handle_close)
        self.server.set_fn_message_received(self.handle)

        logged = False
        while not logged:
            try:
                f = open("/ws_code.log", "w")
                f.write("websocket_code=ready")
                f.close()
                logged = True
            except:
                time.sleep(0.1)

        self.server.run_forever()


# Execute!
if __name__ == "__main__":
    server = Template()
    server.run_server()
