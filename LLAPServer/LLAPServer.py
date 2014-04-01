#!/usr/bin/env python
# -*- coding: utf-8 -*-
""" LLAPServer


"""
import sys
from time import time, sleep, gmtime, strftime
import os
import Queue
import argparse
import ConfigParser
import serial
import threading
import socket
import select
import json
import logging
import AT

"""
   Big TODO list
   
   LCR logic
    DONE: first pass at processing a request in and out
    DONE: check DTY
    DONE: timeouts from config or JSON
    
   DONE: better serial read logic
   
   DONE: Catch Ctrl-C
   DONE: Clean up on quit code
   DONE: Clean up on die code
   
   Thread state monitor
       gpio state display
       GUI
       DONE: restart dead threads
       DONE: restart dead serial
       restart dead socket
   
   "SERVER" messages
        DONE: status
        reboot
        stop
        config change
        
   DONE: Set ATLH1 on start
   make ATLH1 permenent on command line option
   
   
   service launcher
   
   
"""

class LLAPServer():
    """Core logic and master thread control
        
    LLAPServer looks after the following threads
    Serial
    LCR
    UDP Send
    UDP Listen
    
    It starts by loading the LLAPServerConfig.cfg file
    Setting up debug out put and logging
    Then starts the threads for the various transport layers
    
    
    """
    
    _configFile = "./LLAPServer.cfg"
    
    _SerialFailCount = 0
    _SerialFailCountLimit = 3
    _serialTimeout = 1     # serial port time out setting
    _UDPListenTimeout = 5   # timeout for UDP listen
    
    _version = 0.01

    _currentLCR = False
    devType = None
    _SerialDTYSync = False
    _LCRStartTime = 0
    _LCRCurrentTimeout = 0
    
    _validID = "ABCDEFGHIJKLMNOPQRSTUVWXYZ-#@?\\*"
    _validData = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 !\"#$%&'()*+,-.:;<=>?@[\\\/]^_`{|}~"
    
    _state = ""
    RUNNING = "RUNNING"
    ERROR = "ERROR"
    
    def __init__(self, logger=None):
        """Instantiation
            
        Setup basic transport, Queue's, Threads etc
        """
        
        self.tMainStop = threading.Event()
        self.qServer = Queue.Queue()
        
        # setup initial Logging
        logging.getLogger().setLevel(logging.NOTSET)
        self.logger = logging.getLogger('LLAPServer')
        self._ch = logging.StreamHandler()
        self._ch.setLevel(logging.WARN)    # this should be WARN by default
        self._formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self._ch.setFormatter(self._formatter)
        self.logger.addHandler(self._ch)
    
    def __del__(self):
        """Destructor
            
        Close any open threads, and transports
        """
        # TODO: shut down anything we missed
        pass

    def run(self):
        """Start doing everything running
           This is the main entry point
        """
        self.logger.info("Start")
        
        try:
            self._checkArgs()           # pull in the command line options
            self._readConfig()          # read in the config file
            self._initLogging()         # setup the logging options
            self._initLCRThread()       # start the LLAPConfigRequest thread
            self._initUDPSendThread()   # start the UDP sender
            self.tMainStop.wait(1)
            self._initSerialThread()    # start the serial port thread
            self.tMainStop.wait(1)
            self._initUDPListenThread() # start the UDP listener
            
            self._state = self.RUNNING
            
            # main thread looks after the server status for us
            while not self.tMainStop.is_set():
                # check threads are running
                if not self.tLCR.is_alive():
                    self.logger.error("LCR thread stopped")
                    self._state = self.ERROR
                    self.tMainStop.wait(1)
                    self._startLCR()
                    self.tMainStop.wait(1)
                    if self.tLCR.is_alive():
                        self._state = self.RUNNING
            
                if not self.tUDPSend.is_alive():
                    self.logger.error("UDPSend thread stopped")
                    self._state = self.ERROR
                    self.tMainStop.wait(1)
                    self._startUDPSend()
                    self.tMainStop.wait(1)
                    if self.tUDPSend.is_alive():
                        self._state = self.RUNNING
                            
                if not self.tSerial.is_alive():
                    self.logger.error("Serial thread stopped, wait 1 before trying to re-establish ")
                    self._state = self.ERROR
                    self.tMainStop.wait(1)
                    self._startSerail()
                    self.tMainStop.wait(1)
                    if self.tSerial.is_alive():
                        self._state = self.RUNNING
                    else:
                        self._SerialFailCount += 1
                        if self._SerialFailCount > self._SerialFailCountLimit:
                            self.logger.error("Serial thread faile to recover after {} retries, Exiting".format(self._SerialFailCountLimit))
                            self.die()

                if not self.tUDPListen.is_alive():
                    self.logger.error("UDPListen thread stopped")
                    self._state = self.ERROR
                    self.tMainStop.wait(1)
                    self._startUDPListen()
                    self.tMainStop.wait(1)
                    if self.tUDPSend.is_alive():
                        self._state = self.RUNNING
                
                # process any "Server" messages
                if not self.qServer.empty():
                    self.logger.debug("Processing Server JSON")
                    try:
                        self.qServer.get_nowait()
                    except Queue.Empty():
                        pass
                    else:
                        self.qUDPSend.put(json.dumps({"type": "Server", "state": self._state}))
            
                # flash led's if GPIO debug
                
                self.tMainStop.wait(0.5)

        except KeyboardInterrupt:
            self.logger.info("Keyboard Interrupt - Exiting")
            self._clean_up()
            sys.exit()
        self.logger.debug("Exiting")
    
    def _checkArgs(self):
        """Parse the command line options
        """
        parser = argparse.ArgumentParser(description='LLAP Server')
        parser.add_argument('-u', '--noupdate',
                            help='disable checking for update',
                            action='store_false')
        parser.add_argument('-d', '--debug',
                            help='Enable debug output to console, overrides LLAPServer.cfg setting',
                            action='store_true')
        parser.add_argument('-l', '--log',
                            help='Override the debug logging level, DEBUG, INFO, WARNING, ERROR, CRITICAL'
                            )
                            
        self.args = parser.parse_args()

    def _readConfig(self):
        """Read the server config file from disk
        """
        self.logger.info("Reading config files")
        self.config = ConfigParser.SafeConfigParser()
        
        # load defaults
        try:
            self.config.readfp(open(self._configFile))
        except:
            self.logger.error("Could Not Load Settings File")

        if not self.config.sections():
            self.logger.critical("No Config Loaded, Exiting")
            self.die()

    def _initLogging(self):
        """ now we have the config file loaded and the command line args setup
            setup the loggers
        """
        self.logger.info("Setting up Loggers. Console output may stop here")

        # disable logging if no options are enabled
        if (self.args.debug == False and
            self.config.getboolean('Debug', 'console_debug') == False and
            self.config.getboolean('Debug', 'file_debug') == False):
            self.logger.debug("Disabling loggers")
            # disable debug output
            self.logger.setLevel(100)
            return
        # set console level
        if (self.args.debug or self.config.getboolean('Debug', 'console_debug')):
            self.logger.debug("Setting Console debug level")
            if (self.args.log):
                logLevel = self.args.log
            else:
                logLevel = self.config.get('Debug', 'console_level')
        
            numeric_level = getattr(logging, logLevel.upper(), None)
            if not isinstance(numeric_level, int):
                raise ValueError('Invalid console log level: %s' % loglevel)
            self._ch.setLevel(numeric_level)
        else:
            self._ch.setLevel(100)

        # add file logging if enabled
        # TODO: look at rotating log files
        # http://docs.python.org/2/library/logging.handlers.html#logging.handlers.TimedRotatingFileHandler
        if (self.config.getboolean('Debug', 'file_debug')):
            self.logger.debug("Setting file debugger")
            self._fh = logging.FileHandler(self.config.get('Debug', 'log_file'))
            self._fh.setFormatter(self._formatter)
            logLevel = self.config.get('Debug', 'file_level')
            numeric_level = getattr(logging, logLevel.upper(), None)
            if not isinstance(numeric_level, int):
                raise ValueError('Invalid console log level: %s' % loglevel)
            self._fh.setLevel(numeric_level)
            self.logger.addHandler(self._fh)
            self.logger.info("File Logging started")

    def _initLCRThread(self):
        """ Setup the Thread and Queues for handling LLAPConfigRequests
        """
        self.logger.info("LCR Thread init")

        self.qLCRRequest = Queue.Queue()
        self.qLCRSerial = Queue.Queue()
        
        self.tLCRStop = threading.Event()
        self.fAnsweredAll = threading.Event()
        self.fRetryFail = threading.Event()
        self.fTimeoutFail = threading.Event()
        self.fKeepAwake = threading.Event()
        self.fKeepAwake.clear()
        self.fTimeoutFail.clear()
        self.fRetryFail.clear()
        self.fAnsweredAll.clear()

        self._startLCR()
    
    def _startLCR(self):
        self.tLCR = threading.Thread(name='tLCR', target=self._LCRThread)
        self.tLCR.daemon = False

        try:
            self.tLCR.start()
        except:
            self.logger.exception("Failed to Start the LCR thread")
            
    def _initUDPSendThread(self):
        """ Start the UDP output thread
        """
        self.logger.info("UDP Send Thread init")
    
        self.qUDPSend = Queue.Queue()
        
        self.tUDPSendStop = threading.Event()
    
        self.tUDPSend = threading.Thread(name='tUDPSendThread', target=self._UDPSendTread)
        self.tUDPSend.daemon = False
        self._startUDPSend()
        
    def _startUDPSend(self):
        try:
            self.tUDPSend.start()
        except:
            self.logger.exception("Failed to Start the UDP send thread")

    def _initSerialThread(self):
        """ Setup the serial port and start the thread
        """
        self.logger.info("Serial port init")

        # serial port base on config file, thread handles opening and closing
        self._serial = serial.Serial()
        self._serial.port = self.config.get('Serial', 'port')
        self._serial.baud = self.config.get('Serial', 'baudrate')
        self._serial.timeout = self._serialTimeout
        
        # setup queue
        self.qSerialOut = Queue.Queue()
        self.qSerialToQuery = Queue.Queue()
        
        # setup thread
        self.tSerialStop = threading.Event()
        
        self._startSerail()
    
    def _startSerail(self):
        self.tSerial = threading.Thread(name='tSerial', target=self._SerialThread)
        self.tSerial.daemon = False
    
        try:
            self.tSerial.start()
        except:
            self.logger.exception("Failed to Start the Serial thread")

    def _initUDPListenThread(self):
        """ Start the UDP Listen thread and queues
        """
        self.logger.info("UDP Listen Thread init")

        self.tUDPListenStop = threading.Event()

        self.tUDPListen = threading.Thread(name='tUDPListen', target=self._UDPListenThread)
        self.tUDPListen.deamon = False
        
        self._startUDPListen()
        
    def _startUDPListen(self):
        try:
            self.tUDPListen.start()
        except:
            self.logger.exception("Failed to Start the UDP listen thread")

    def _LCRThread(self):
        """ LLAP Config Request thread
            Main logic for dealing with LCR's
            We check the incoming qLCRRequest and qLCRSerial
        """
        self.logger.info("tLCR: LCR thread started")
        
        while (not self.tLCRStop.is_set()):
            # do we have a request
            if not self.qLCRRequest.empty():
                self.logger.debug("tLCR: Got a request to process")
                # if we are not in the middle of an LCR
                # TODO: what if its a cancel (shouldn't need them with timeouts
                if not self._currentLCR:
                    # lets get it out the queue and start processing it
                    try:
                        self._currentLCR = self.qLCRRequest.get_nowait()
                    except Queue.Empty:
                        self.logger.debug("tLCR: Failed to get item from qLCRRequest")
                    else:
                        # check the keepAwake
                        if self._currentLCR['data'].get('keepAwake', None) == 1:
                            self.logger.debug("tLCR: keepAwake turned on")
                            self.fKeepAwake.set()
                        elif self._currentLCR['data'].get('keepAwake', None) == 0:
                            self.logger.debug("tLCR: keepAwake turned off")
                            self.fKeepAwake.clear()
                        
                        if self._currentLCR['data'].get('toQuery', False):
                            # make place for replies later
                            self._currentLCR['data']['replies'] = {}
                            # pass queries on to the serial thread to send out
                            try:
                                self.qSerialToQuery.put_nowait(self._currentLCR['data']['toQuery'])
                            except Queue.Full:
                                self.logger.debug("tLCR: Failed to put item onto toQuery as it's full")
                            else:
                                self.devType = self._currentLCR['data'].get('devType', None)
                                # reset flags
                                self.fAnsweredAll.clear()
                                self.fRetryFail.clear()
                                self.fTimeoutFail.clear()
                                # start timer
                                self._LCRCurrentTimeout = int(self._currentLCR['data'].get('timeout', self.config.get('LCR', 'timeout')))
                                self._LCRStartTime = time()
                                self.logger.debug("tLCR: started LCR timeout with period: {}".format(self._LCRCurrentTimeout))
                        else:
                            # no toQuery section, so reply with all done
                            self._LCRReturnLCR("PASS")
                        self.qLCRRequest.task_done()

            # do we have a reply from serial
            while not self.qLCRSerial.empty():
                self.logger.debug("tLCR: Something in qLCRSerial")
                try:
                    llapReply = self.qLCRSerial.get_nowait()
                except Queue.Empty:
                    self.logger.debug("tLCR: Failed to get item from qLCRSerial")
                else:
                    self.logger.debug("tLCR: Got {} to process".format(llapReply))
                    if self._currentLCR:
                        # we are working on a request check and store the reply
                        for q in self._currentLCR['data']['toQuery']:
                            if llapReply.strip('-').startswith(q['command']):
                                self._currentLCR['data']['replies'][q['command']] = {'value': q.get('value', ""),
                                                                                'reply': llapReply[len(q['command']):].strip('-')
                                                                                }
                                self.logger.debug("tLCR: Stored reply '{}':{}".format(q['command'], self._currentLCR['data']['replies'][q['command']]))
                        # and reset the timeout
                        self.logger.debug("tLCR: Reset timeout to 0")
                        self._LCRStartTime = time()
                    else:
                        # drop it
                        pass
                    self.qLCRSerial.task_done()
            
            # check the timeout
            if self._currentLCR and ((time() - self._LCRStartTime) > self._LCRCurrentTimeout):
                # if expired cancel the toQuery in tSerial
                self.logger.debug("tLCR: LCR timeout expired")
                self.fTimeoutFail.set()

            # no point checking flags if we are not in the middle of a request
            if self._currentLCR:
                # has the serial thread finished getting all the query answers
                if self.fAnsweredAll.is_set():
                    # finished toQuery ok
                    self.logger.debug("tLCR: Serial answered so send out json")
                    self._LCRReturnLCR("PASS")
                elif self.fRetryFail.is_set():
                    # failed due to a message retry issue
                    self.logger.warn("tLCR: Failed current LCR due to retry count")
                    self._LCRReturnLCR("FAIL_RETRY")
                elif self.fTimeoutFail.is_set():
                    # failed due to expired timeout
                    self.logger.warn("tLCR: Failed current LCR due to timeout")
                    while not self.qSerialToQuery.empty():
                        try:
                            self.qSerialToQuery.get()
                            self.logger.debug("tLCR: removed stale query from qSerialToQuery")
                        except Queue.Empty:
                            pass
                    
                    self._LCRReturnLCR("FAIL_TIMEOUT")
            
            # wait a little
            self.tLCRStop.wait(0.5)
            
        self.logger.info("tLCR: Thread stopping")
        return

    def _LCRReturnLCR(self, state):
        # prep the reply
        self._currentLCR['timestamp'] = strftime("%d %b %Y %H:%M:%S +0000", gmtime())
        self._currentLCR['network'] = self.config.get('Serial', 'network')
        self._currentLCR['keepAwake'] = 1 if self.fKeepAwake.is_set() else 0
        self._currentLCR['state'] = state

        # encode json
        jsonout = json.dumps(self._currentLCR)

        # send to UDP thread
        try:
            self.qUDPSend.put_nowait(jsonout)
        except Queue.Full:
            self.logger.warn("tLCR: Failed to put {} on qUDPSend as it's full".format(llapMsg))
        else:
            self.logger.debug("tLCR: Sent LCR reply to qUDPSend")
            # and clear LCR and SentAll flag
            self._currentLCR = False

    def _UDPSendTread(self):
        """ UDP Send thread
        """
        self.logger.info("tUDPSend: Send thread started")
        # setup the UDP send socket
        try:
            UDPSendSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except socket.error, msg:
            self.logger.critical("tUDPSend: Failed to create socket, Exiting. Error code : {} Message : {} ".format(msg[0], msg[1]))
            self.die()
        
        UDPSendSocket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        UDPSendSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        sendPort = int(self.config.get('UDP', 'send_port'))
        
        while (not self.tUDPSendStop.is_set()):
            try:
                message = self.qUDPSend.get(timeout=1)     # block for up to 1 seconds
            except Queue.Empty:
                # UDP Send queue was empty
                # extrem debug message
                # self.logger.debug("tUDPSend: queue is empty")
                pass
            else:
                self.logger.debug("tUDPSend: Got json to send: {}".format(message))
                try:
                    UDPSendSocket.sendto(message, ('<broadcast>', sendPort))
                    self.logger.debug("tUDPSend: Put message out via UDP")
                except socket.error, msg:
                    self.logger.warn("tUDPSend: Failed to send via UDP. Error code : {} Message: {}".format(msg[0], msg[1]))
                
                # tidy up
                self.qUDPSend.task_done()

            # TODO: tUDPSend thread is alive, wiggle a pin?

        self.logger.info("tUDPSend: Thread stopping")
        try:
            UDPSendSocket.close()
        except socket.error:
            self.logger.exception("tUDPSend: Failed to close socket")
        return
    
    def _SerialThread(self):
        """ Serial Thread
        """
        self.logger.info("tSerial: Serial thread started")
        self._SerialToQueryState = 0
        self._SerialToQuery = []
        self.tSerialStop.wait(1)
        try:
            while (not self.tSerialStop.is_set()):
                # open the port
                try:
                    self._serial.open()
                    self.logger.info("tSerial: Opened the serial port")
                except serial.SerialException:
                    self.logger.exception("tSerial: Failed to open port {} Exiting".format(self._serial.port))
                    self._serial.close()
                    self.die()
                
                self.tSerialStop.wait(0.1)
                
                # we clear out any stale serial messages that might be in the buffer
                self._serial.flushInput()
                
                # check the ATLH settings
                self._SerialCheckATLH()
                
                # main serial processing loop
                while self._serial.isOpen() and not self.tSerialStop.is_set():
                    # extrem debug message
                    # self.logger.debug("tSerial: check serial port")
                    if self._serial.inWaiting():
                        self._SerialReadIncomingLLap()
                    
                    # do we have anything to send
                    if not self.qSerialOut.empty():
                        self.logger.debug("tSerial: got something to send")
                        try:
                            llapMsg = self.qSerialOut.get_nowait()
                            self._serial.write(llapMsg)
                        except Queue.Empty:
                            self.logger.debug("tSerial: failed to get item from queue")
                        except Serial.SerialException, e:
                            self.logger.warn("tSerial: failed to write to the serial port {}: {}".format(self._serial.port, e))
                        else:
                             self.logger.debug("tSerial: TX:{}".format(llapMsg))
                             self.qSerialOut.task_done()
                
                    # sleep for a little
                    if self._SerialToQueryState or self._serial.inWaiting():
                        self.tSerialStop.wait(0.01)
                    else:
                        self.tSerialStop.wait(0.1)
                
                # port closed for some reason (or tSerialStop), if tSerialStop is not set we will try reopening
        except IOError:
            self.logger.exception("tSerail: IOError on serial port")
        
        # close the port
        self.logger.info("tSerial: Closing serial port")
        self._serial.close()
        
        self.logger.info("tSerial: Thread stoping")
        return
                    
    def _SerialCheckATLH(self):
        """ check and posible set the the ATLH setting on the radio
            if command line XX the make permenant (ATWR)
        """
        self.logger.info("tSerial: Setting ATLH1")

        self._serial.flushInput()
    
        at = AT.AT(self._serial, self.logger, self.tSerialStop)
    
        if at.enterATMode():
            if at.sendATWaitForOK("ATLH1"):
                if 0:
                    at.sendATWaitForOK("ATWR")
        
            at.leaveATMode()
    
    def _SerialReadIncomingLLap(self):
        char = self._serial.read()  # should not time out but we should check anyway
        self.logger.debug("tSerial: RX:{}".format(char))
    
        if char == 'a':
            # this should be the start of a llap message
            # read 11 more or time out
            llapMsg = "a"
            count = 0
            while count < 11:
                char = self._serial.read()
                if not char:    # TODO: check this is right for a time out
                    self.logger.debug("tSerial: RX:{}".format(char))
                    return
            
                if char == 'a':
                    # start again and
                    count = 0
                    llapMsg = "a"
                    self.logger.debug("tSerial: RX:{}".format(char))
                elif (count == 0 or count == 1) and char in self._validID:
                    # we have a vlaid ID
                    llapMsg += char
                    count += 1
                elif count >= 2 and char in self._validData:
                    # we have a valid data
                    llapMsg += char
                    count +=1
                else:
                    self.logger.debug("tSerial: RX:{}".format(llapMsg[1:] + char))
                    return
    
            self.logger.debug("tSerial: RX:{}".format(llapMsg[1:]))
            
            if len(llapMsg) == 12:  # just double check length
                if llapMsg[1:3] == "??":
                    self._SerialProcessQQ(llapMsg[3:].strip("-"))
                else:
                    # not a configme llap so send out via UDP LLAP
                    try:
                        self.qUDPSend.put_nowait(self.encodeLLAPJson(llapMsg, self.config.get('Serial', 'network')))
                    except Queue.Full:
                        self.logger.warn("tSerial: Failed to put {} on qUDPSend as it's full".format(llapMsg))

    def _SerialProcessQQ(self, llapMsg):
        """ process an incoming ?? llap message
        """
        # has the timeout expired
        if not self.fTimeoutFail.is_set():
            if self._SerialToQueryState:
                # was it a reply to our DTY test
                if self.devType and (not self._SerialDTYSync):
                    # we should have a reply to DTY
                    if llapMsg.startswith("DTY"):
                        if llapMsg[3:] == self.devType:
                            self._SerialDTYSync = True
                            self.logger.debug("tSerial: Confirmed DTY, Send next toQuery, State: {}".format(self._SerialToQueryState))
                            if not self._SerialSendLCRQuery():
                                # failed to send question (serial or retry error)
                                if self.fRetryFail.is_set():
                                    # was a retry fail
                                    # stop processing toQuery
                                    self._SerialToQueryState = 0
                            return
        
                # check reply was to the last question
                if llapMsg.startswith(self._SerialToQuery[self._SerialToQueryState-1]['command']):
                    # reduce the state count and reset retry count
                    self._SerialToQueryState -= 1
                    self._SerialRetryCount = 0
                    
                    # store the reply
                    try:
                        self.qLCRSerial.put_nowait(llapMsg)
                    except Queue.Full:
                        self.logger.warn("tSerial: Failed to put {} on qLCRSerial as it's full".format(llapMsg))
                    
                    # if we have replies for all state == 0:
                    if self._SerialToQueryState == 0:
                        # sent and received all
                        self.fAnsweredAll.set()
                        self.logger.debug("tSerial: Go answers for all toQuery")
                    # else we have a another query to send
                    else:
                        # send next
                        self.logger.debug("tSerial: Send next toQuery, State: {}".format(self._SerialToQueryState))
                        if not self._SerialSendLCRQuery():
                            # failed to send question (serial error)
                            pass
                # else if was not our answer so send it again
                elif llapMsg == "CONFIGME":
                    if self.devType:
                        # out of sync should we recheck DTY?
                        self._SerialDTYSync = False
                        self.logger.debug("tSerial: Checking DTY again before sending next toQuery")
                        self._SerialSendDTY()
                    else:
                        # send last again
                        self.logger.debug("tSerial: Retry toQuery, State: {}".format(self._SerialToQueryState))
                        if not self._SerialSendLCRQuery():
                            # failed to send question (serial or retry error)
                            if self.fRetryFail.is_set():
                                # was a retry fail
                                # stop processing toQuery
                                self._SerialToQueryState = 0
                                return
        
            elif llapMsg == "CONFIGME":
                # do we have a waiting query and can we send one
                try:
                    self._SerialToQuery = self.qSerialToQuery.get_nowait()
                except Queue.Empty:
                    pass
                else:
                    self._SerialToQuery.reverse()
                    self._SerialToQueryState = len(self._SerialToQuery)
                    self.fAnsweredAll.clear()
                    self.fRetryFail.clear()
                    # clear retry count
                    self._SerialRetryCount = 0
                    # new query should we check DTY
                    if self.devType:
                        self.logger.debug("tSerial: Checking DTY before sending first toQuery")
                        self._SerialSendDTY()
                    else:
                        # send first
                        self.logger.debug("tSerial: Send first toQuery, State: {}".format(self._SerialToQueryState))
                        if not self._SerialSendLCRQuery():
                            # failed to send question (serial or retry error)
                            pass
        elif self._SerialToQueryState:
            # yes the time out expired, clear down any current toQuery
            self.logger.debug("tSerial: toQuery Timmed out")
            self._SerialToQueryState = 0
        
            
        # only thing left now would be a CONFIGME so do we need to send a keepAwake
        if llapMsg == "CONFIGME" and self.fKeepAwake.is_set():
            try:
                self._serial.write("a??HELLO----")
            except Serial.SerialException, e:
                self.logger.warn("tSerial: failed to write to the serial port {}: {}".format(self._serial.port, e))
            else:
                self.logger.debug("tSerial: TX:a??HELLO-----")
            return

    def _SerialSendLCRQuery(self):
        """ send out the next query in the current LCR
        """
        # check retry count before sending
        if self._SerialRetryCount < int(self.config.get('LCR', 'single_query_retry_count')):
            llapToSend = "a??{}{}".format(self._SerialToQuery[self._SerialToQueryState-1]['command'],
                                           self._SerialToQuery[self._SerialToQueryState-1].get('value', "")
                                           )
            while len(llapToSend) < 12:
                llapToSend += "-"
            try:
                self._serial.write(llapToSend)
            except Serial.SerialException, e:
                self.logger.warn("tSerial: failed to write to the serial port {}: {}".format(self._serial.port, e))
                return False
            else:
                self.logger.debug("tSerial: TX:{}".format(llapToSend))
                self._SerialRetryCount += 1
                return True
        self.logger.debug("tSerial: toQuery failed on retry count, letting tLCR know")
        self.fRetryFail.set()
        return False

    def _SerialSendDTY(self):
        """ Ask a LLAP+ device it devType
        """
        try:
          self._serial.write("a??DTY------")
        except Serial.SerialException, e:
          self.logger.warn("tSerial: failed to write to the serial port {}: {}".format(self._serial.port, e))
          return False
        else:
          self.logger.debug("tSerial: TX:a??DTY------")
          self._SerialDTYSync = False
          return True

    def _UDPListenThread(self):
        """ UDP Listen Thread
        """
        self.logger.info("tUDPListen: UDP listen thread started")
        
        try:
            UDPListenSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except socket.error:
            self.logger.exception("tUDPListen: Failed to create socket, Exiting")
            self.die()

        UDPListenSocket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        UDPListenSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if (self.args.debug) and sys.platform == 'darwin':
            UDPListenSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        
        try:
            UDPListenSocket.bind(('', int(self.config.get('UDP', 'listen_port'))))
        except socket.error:
            self.logger.exception("tUDPListen: Failed to bind port, Exiting")
            self.die()
        
        UDPListenSocket.setblocking(0)
        
        self.logger.info("tUDPListen: listening")
        while not self.tUDPListenStop.is_set():
            datawaiting = select.select([UDPListenSocket], [], [], self._UDPListenTimeout)
            if datawaiting[0]:
                (data, address) = UDPListenSocket.recvfrom(1024)
                self.logger.debug("tUDPListen: Received JSON: {} From: {}".format(data, address))
                jsonin = json.loads(data)
                
                if jsonin['type'] == "LLAP":
                    self.logger.debug("tUDPListen: JSON of type LLAP, send out messages")
                    # got a LLAP type json, need to generate the LLAP message and
                    # put them on the TX que
                    for command in jsonin['data']:
                        llapMsg = "a{}{}".format(jsonin['id'], command[0:9].upper())
                        while len(llapMsg) <12:
                            llapMsg += '-'
                        
                        # send to each network requested
                        if (jsonin['network'] == self.config.get('Serial', 'network') or
                            jsonin['network'] == "ALL"):
                            # yep its for serial
                            try:
                                self.qSerialOut.put_nowait(llapMsg)
                            except Queue.Full:
                                self.logger.debug("tUDPListen: Failed to put {} on qLCRSerial as it's full".format(llapMsg))
                            else:
                                self.logger.debug("tUDPListen Put {} on qSerialOut".format(llapMsg))

                elif jsonin['type'] == "LCR":
                    # we have a LLAPConfigRequest pass in onto the LCR thread
                    self.logger.debug("tUDPListen: JSON of type LCR, passing to qLCRRequest")
                    try:
                        self.qLCRRequest.put_nowait(jsonin)
                    except Queue.Full:
                        self.logger.debug("tUDPListen: Failed to put json on qLCRRequest")

                elif jsonin['type'] == "Server":
                    # TODO: we have a SERVER json do stuff with it
                    self.logger.debug("tUDPListen: JSON of type SERVER, passing to qServer")
                    try:
                        self.qServer.put(jsonin)
                    except Queue.Full():
                        self.logger.debug("tUDPListen: Failed to put json on qServer")
 
        self.logger.info("tUDPListen: Thread stopping")
        try:
            UDPListenSocket.close()
        except socket.error:
            self.logger.exception("tUDPListen: Failed to close socket")
        return
                
    def encodeLLAPJson(self, message, network=None):
        """Encode a single LLAP message into an outgoing JSON message
            """
        self.logger.debug("tSerial: JSON: encoding {} to json LLAP".format(message))
        jsonDict = {'type':"LLAP"}
        jsonDict['network'] = network if network else "DEFAULT"
        jsonDict['timestamp'] = strftime("%d %b %Y %H:%M:%S +0000", gmtime())
        jsonDict['id'] = message[1:3]
        jsonDict['data'] = [message[3:].strip("-")]

        jsonout = json.dumps(jsonDict)
        # extrem debugging
        # self.logger.debug("JSON: {}".format(jsonout))

        return jsonout

    def _clean_up(self):
        """ clean up on exit
        """
        # first stop the main thread from try to restart stuff
        self.tMainStop.set()
        # now stop the other threads
        try:
            self.tUDPListenStop.set()
            self.tUDPListen.join()
        except:
            pass
        try:
            self.tSerialStop.set()
            self.tSerial.join()
        except:
            pass
        try:
            self.tLCRStop.set()
            self.tLCR.join()
        except:
            pass
        try:
            self.tUDPSendStop.set()
            self.tUDPListen.join()
        except:
            pass
        
    def die(self):
        """For some reason we can not longer go forward
            Try cleaning up what we can and exit
        """
        self.logger.critical("DIE")
        self._clean_up()

        sys.exit(1)

# run code
if __name__ == "__main__" :
    app = LLAPServer()
    app.run()
