from __future__ import annotations

__all__ = ['MAVCom', 'test_MAVCom']

import time, os, sys
import typing

from ..logging import logging, LogLevels
from ..utils.general import LeakyQueue, format_rcvd_msg, time_since_boot_ms, time_UTC_usec, date_time_str

assert os.environ[
           'MAVLINK20'] == '1', "Set the environment variable before from pymavlink import mavutil  library is imported"


import threading
import queue
import typing as typ
from pathlib import Path

from pymavlink import mavutil

# from mavcom.imports import *   # TODO why is this relative import on nbdev_export?
# from .component import Component
# from .camera_server import CameraServer, Component
# from .camera_client import CameraClient
# from .gimbal_client import GimbalClient
# from .vs_gimbal import GimbalClient

# def get_linenumber():
#     cf = currentframe()
#     filename = Path(getframeinfo(cf).filename).name
#     return f"{filename}:{cf.f_back.f_lineno}"

#
# def format_rcvd_msg(msg, extra=''):
#     """ Format a message for logging."""
#     s = f"{str(msg)} ... {extra}"
#     try:
#         s = f"Rcvd {msg.get_srcSystem():3d}/{msg.get_srcComponent():3d} {s}"
#     except:
#         try:
#             s = f"Rcvd {'???'}/{msg.get_srcComponent():3d} {s}"
#         except:
#             s = f"Rcvd {'???'}/{'???'} {s}"
#     return s

# boot_time = time.time()
# boot_time_str = time.strftime("%Y-%m-%d|%H:%M:%S", time.localtime(boot_time))
# def time_since_boot_ms()->int:
#     """Return the time since boot in milliseconds."""
#     # try:
#     #     a = boot_time
#     # except NameError:
#     #     boot_time = time.time()
#     return int((time.time() - boot_time) * 1000)
#
# def time_UTC_usec()->int:
#     return int(time.time() * 1e6)
#
# def date_time_str()->str:
#     return time.strftime("%Y-%m-%d|%H:%M:%S", time.localtime())
#


MAV_SYSTEM_GCS_CLIENT = 200  # GCS type client (TODO its not clear if this is correct,  255 = GCS)
MAV_TYPE_GCS = mavutil.mavlink.MAV_TYPE_GCS
MAV_SYSTEM_VEHICLE = 111  # 1 = vehicle
MAV_TYPE_CAMERA = mavutil.mavlink.MAV_TYPE_CAMERA
MAV_COMP_ID_CAMERA = mavutil.mavlink.MAV_COMP_ID_CAMERA
MAV_COMP_ID_USER1 = mavutil.mavlink.MAV_COMP_ID_USER1



class _BaseComponent:
    """Create a mavlink Component with an ID  for MAV_COMPONENT"""

    def __init__(self, mav_connection,  # MavLinkBase connection
                 source_component,  # used for component indication
                 mav_type,  # used for heartbeat MAV_TYPE indication
                 debug):  # logging level
        # todo change to def __init__(self:MavLinkBase, ....
        self.mav_connection: MAVCom = None
        self.master = None
        self.mav_type = mav_type
        self.source_system = self.mav_connection.source_system
        self.source_component = source_component

        self._log = logging.getLogger("mavcom.{}".format(self.__class__.__name__))
        self._log.setLevel(logging.DEBUG if debug else logging.INFO)

        self.boot_time = time_since_boot_ms()
        self.ping_num = 0
        self.max_pings = 4
        self.num_msgs_rcvd = 0
        # self.num_cmds_sent = 0
        # self.num_cmds_rcvd = 0
        # self.num_acks_sent     = 0
        # self.num_acks_rcvd = 0
        # self.num_acks_drop = 0
        self.message_cnts: {} = {}  # received message counts, indexed by system and message type
        # 
        self._heartbeat_que = LeakyQueue(maxsize=100, log=self.log)
        self._ack_que = LeakyQueue(maxsize=100, log=self.log)
        self._message_que = LeakyQueue(maxsize=100, log=self.log)

        self._t_heartbeat = threading.Thread(target=self.send_heartbeat, daemon=True)
        self._t_heartbeat.start()

        self._t_msg_listen = threading.Thread(target=self.listen, daemon=True)  # todo rename to _t_msg_listen
        self._t_msg_listen.start()
        self._t_msg_listen.name = f"{self.__class__.__name__}._t_msg_listen"
        # self.log.info(f"Component Started {self.source_component = }, {self.mav_type = }, {self.source_system = }")

    def __str__(self) -> str:
        return self.__class__.__name__

    def __repr__(self) -> str:
        return "<{}>".format(self)

    @property
    def log(self) -> logging.Logger:
        return self._log

    def set_mav_connection(self, mav_connection):
        """Set the mav_connection for the component"""
        self.mav_connection = mav_connection
        self.master = mav_connection.master
        self.mav = self.master.mav

    def set_source_compenent(self):
        """Set the source component for the master.mav """
        self.master.mav.srcComponent = self.source_component

    def send_ping(self, target_system: int, target_component: int, max_pings=None):
        """Send self.max_pings * ping messages to test if the server is alive."""

        if max_pings is not None: self.max_pings = max_pings
        if self.ping_num >= self.max_pings:
            self.ping_num = 0
            return
        self.set_source_compenent()
        self.master.mav.ping_send(
            int(time.time() * 1000),  # Unix time 
            self.ping_num,  # Ping number
            target_system,  # Request ping of this system
            target_component,  # Request ping of this component
        )
        self.log.info(f"Sent Ping #{self.ping_num} to:   {target_system:3d}, comp: {target_component:3d}")
        self.ping_num += 1

    def send_heartbeat(self):
        """Send a heartbeat message to indicate the server is alive."""
        self._t_heartbeat_stop = False

        # self.log.info(f"Starting heartbeat type: {self.mav_type} to all Systems and Components")
        while not self._t_heartbeat_stop:
            self.set_source_compenent()
            # self.log.debug(f"Sent hrtbeat to All")
            self.master.mav.heartbeat_send(
                self.mav_type,  # type
                # mavutil.mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,  # autopilot
                0,  # base_mode
                0,  # custom_mode
                mavutil.mavlink.MAV_STATE_ACTIVE,  # system_status
            )
            time.sleep(1)  # Send every second

    def count_message(self, msg):
        """ Count a message by adding it to the message_cnts dictionary. indexed by system and message type"""
        try:
            self.message_cnts[msg.get_srcSystem()][msg.get_type()] += 1
        except Exception as e:
            # print(f"!!!! new Message type {msg.get_type()} from system {msg.get_srcSystem()}")
            _sys = msg.get_srcSystem()
            if _sys not in self.message_cnts:
                self.message_cnts[_sys] = {}
            self.message_cnts[_sys][msg.get_type()] = 1

        return True

    def listen(self, timeout: int = 1, ):  # seconds
        """Listen for MAVLink commands and trigger the cameras when needed."""

        self._t_msg_listen_stop = False
        # self.log.info(f"Component Listening for messages sent on the message_queue ...")
        while not self._t_msg_listen_stop:

            try:
                msg = self._message_que.get(timeout=timeout)
                # self.log.info(format_rcvd_msg(msg))

                self.num_msgs_rcvd += 1
            except queue.Empty:  # i.e time out
                time.sleep(0.01)
                continue

            self.count_message(msg)
            if msg.get_type() == 'PING':
                # self.log.debug(f"Received PING {msg}")
                # ping_num = msg.time_usec
                ping_num = msg.seq
                # print(f"{ping_num = } {msg}")
                if ping_num < self.max_pings:
                    self.log.debug(f"Received PING {msg}")
                    self.send_ping(msg.get_srcSystem(), msg.get_srcComponent())
            else:
                self.on_message(msg)

    def on_message(self, msg):
        """Process a message. """
        if msg.get_type() == 'COMMAND_LONG':
            # print("Om command ")
            self.on_command_rcvd(msg)
        elif msg.get_type() == 'COMMAND_INT':
            self.on_command_rcvd(msg)

        elif msg.get_type() == 'COMMAND_ACK':
            self.log.debug(f"Received ACK ")
            self._ack_que.put(msg, block=False)
            self.on_ack(msg)

        elif msg.get_type() == 'HEARTBEAT':
            # self.log.debug(f"Received HEARTBEAT ")
            self._heartbeat_que.put(msg, block=False)
            self.on_heartbeat(msg)

        elif msg.get_type() == 'PING':
            # self.log.debug(f"Received PING {msg}")
            # ping_num = msg.time_usec
            ping_num = msg.seq
            # print(f"{ping_num = } {msg}")
            if ping_num < self.max_pings:
                self.log.debug(f"Received PING {msg}")
                self.send_ping(msg.get_srcSystem(), msg.get_srcComponent())

    def on_command_rcvd(self, msg):
        """Process a command message, Please subclass. """
        self.log.error(f"Please subclass: Received {msg.get_type() = }")
        pass

    def on_heartbeat(self, msg):
        """Process a heartbeat message, Please subclass. """
        self.log.error(f"Please subclass: Received {msg.get_type() = }")
        pass

    def on_ack(self, msg):
        """Process an ack message, Please subclass. """
        self.log.error(f"Please subclass: Received {msg.get_type() = }")
        pass

    def close(self):
        self._t_msg_listen_stop = True
        self._t_heartbeat_stop = True
        self._t_msg_listen.join()
        self._t_heartbeat.join()
        self.log.info(f"{self.__class__.__name__} closed")


class MAVCom:
    """
    Mavlink Base to set up a mavlink_connection for send and receive messages to and from a remote system.
    """

    def __init__(self, connection_string: str,  # "udpin:localhost:14550"
                 baudrate: int = 57600,  # baud rate of the serial port
                 source_system: int = MAV_SYSTEM_VEHICLE,  # remote or air system   1 = vehicle
                 loglevel: LogLevels | int = LogLevels.INFO,  # logging level
                 ):

        self._log = logging.getLogger("mavcom.{}".format(self.__class__.__name__))

        self._log.setLevel(int(loglevel))
        self.connection_string: str = connection_string
        self.baudrate: int = baudrate
        self.source_system: int = source_system

        self.check_message_route(None)

        self.message_cnts: {} = {}  # received message counts, indexed by system and message type
        self.component: {_BaseComponent} = {}  # todo fix this typing
        self._t_mav_listen_stop = True
        self._t_mav_listen = None
        self.do_ack = True
        self.start_mavlink()

    def start_mavlink(self):
        """Start the MAVLink connection."""

        try:
            self.master = mavutil.mavlink_connection(self.connection_string,  # "udpin:localhost:14550"
                                                    baud=self.baudrate,  # baud rate of the serial port
                                                    source_system=int(self.source_system),  # source system
                                                    )
        except Exception as e:
            self.log.error(e)
            raise e
            return

        # self.log.info(f"see https://mavlink.io/en/messages/common.html#MAV_COMPONENT")
        time.sleep(0.1)  # Todo delay for connection to establish

        assert hasattr(self, 'master'), "start_mavlink() must be called before threading.Thread(target=self.listen..."

        self._t_mav_listen = threading.Thread(target=self.listen, daemon=True)
        self._t_mav_listen.start()
        assert self.master.mavlink20(), "Mavlink 2 protocol is not happening ?, check os.environ['MAVLINK20'] = '1'"
        pass

    def on_message(self, msg):
        pass  # overide this

    def start_listen(self):
        """Listen for MAVLink commands """
        self._t_mav_listen.start()

    def __str__(self) -> str:
        return self.__class__.__name__

    def __repr__(self) -> str:
        return "<{}>".format(self)

    @property
    def log(self) -> logging.Logger:
        return self._log

    def check_message_route(self, msg,  # message
                            hide_log=False,  # hide log output for messages
                            ) -> typ.Union[int, None]:
        """
               check message and routing. return the component ID to be routed to , 0 = all, None = none.

                Systems/components should process a message if any of these conditions hold:
                   - It is a broadcast message (target_system field omitted or zero)
                   - The target_system matches its system id and target_component is broadcast (target_component omitted or zero).
                   - The target_system matches its system id and has the component's target_component or
                   - The target_system matches its system id and the component is unknown (i.e. this component has not seen any messages on any link that have the message's target_system/target_component)
               """

        if msg is None:
            return None

        self.count_message(msg)

        if msg.get_type() == "BAD_DATA":
            return None

        # Test - It is a broadcast message (target_system field omitted or zero)
        if not hasattr(msg, 'target_system') or msg.target_system == 0:
            # if msg.get_type() != 'HEARTBEAT':
            #     self.log.debug(format_rcvd_msg(msg, "target_system: ???, Pass to ALL Components"))
            return 0  # for all components

        # Test - The target_system matches its system id and target_component is broadcast (target_component omitted or zero).
        #      - The target_system matches its system id and has the component's target_component
        if msg.target_system == self.source_system:
            if hasattr(msg, 'target_component'):
                self.log.debug(format_rcvd_msg(msg, f"Pass to Component {msg.target_component:3d}"))

                return msg.target_component
            else:
                self.log.debug(format_rcvd_msg(msg, " Pass to ALL Components {'  0'}"))
                return 0

        self.log.error(format_rcvd_msg(msg, "!!!!! No match !!!! {msg.target_system} != {self.source_system}"))

    def count_message(self, msg):
        """ Count a message by adding it to the message_cnts dictionary. indexed by system and message type"""
        try:
            self.message_cnts[msg.get_srcSystem()][msg.get_type()] += 1
        except Exception as e:
            # print(f"!!!! new Message type {msg.get_type()} from system {msg.get_srcSystem()}")
            _sys = msg.get_srcSystem()
            if _sys not in self.message_cnts:
                self.message_cnts[_sys] = {}
            self.message_cnts[_sys][msg.get_type()] = 1

        return True

    def listen(self):
        """Listen for MAVLink commands and trigger the cameras when needed."""
        assert hasattr(self, 'master'), "start_mavlink() must be called before threading.Thread(target=self.listen..."

        self._t_mav_listen_stop = False

        self.log.info(
            f"MAVLink Mav2: {mavutil.mavlink20()}, source_system: {self.source_system}")

        while not self._t_mav_listen_stop:
            # Wait for a MAVLink message
            try:  # Todo: catch bad file descriptor error
                msg = self.master.recv_match(blocking=True, timeout=1)
                # if callable(self.on_message):
                self.on_message(msg)
            except Exception as e:
                self.log.debug(f"Exception: {e}")
                time.sleep(1)
                continue
            comp_ID = self.check_message_route(msg)
            # print(msg)

            if comp_ID is None:
                continue  # don't process message
            if comp_ID == 0:
                # send to all components
                for key, comp in self.component.items():
                    # if msg.get_srcSystem() not in comp.exclude_msgs:
                    comp.message_que.put(msg, block=False)

            else:  # send to specific component
                try:
                    # self.log.info(format_rcvd_msg(msg, f"Pass to Component {comp_ID}"))
                    self.component[comp_ID].message_que.put(msg, block=False)
                except Exception as e:
                    self.log.error(f" Component {comp_ID} does not exist? ; Exception: {e}")
                    continue

    def add_component(self, comp: Component | CameraClient | CameraServer  # commponent to add
                      ) -> Component | CameraClient | CameraServer | GimbalClient | None:  # return the component
        # append a component to the component dictionary with the key being the source_component
        # Check to see if {comp.source_component = } already exists

        if comp.source_component in self.component:
            self.log.error(f"Component {comp.source_component = } already exists")
            assert False, f"Component {comp.source_component = } already exists"
            # return None

        comp.set_mav_connection(self)
        self.component[comp.source_component] = comp
        return comp

    def close(self):
        # print(f"Closing {self.__class__.__name__}...")

        self._t_mav_listen_stop = True
        self._t_mav_listen.join()
        for key, comp in self.component.items():
            comp.close()

        self.master.close()
        self.master.port.close()
        self.log.info(f"{self.__class__.__name__}  closed")

    def summary(self):
        """Return a summary of the component's received message counts"""
        name = self.__class__.__name__
        summary = []
        for key, comp in self.component.items():
            summary.append(f" - {comp.source_component = }")
            summary.append(f" - {comp.num_msgs_rcvd = }")
            summary.append(f" - {comp.num_cmds_sent = }")
            summary.append(f" - {comp.num_cmds_rcvd = }")
            summary.append(f" - {comp.num_acks_rcvd = }")
            summary.append(f" - {comp.num_acks_sent = }")
            summary.append(f" - {comp.num_acks_drop = }")
            summary.append(f" - {comp.message_cnts = }")
        return "\n".join(summary)

    def __enter__(self):
        """ Context manager entry point for with statement."""
        return self  # This value is assigned to the variable after 'as' in the 'with' statement

    def __exit__(self, exc_type, exc_value, traceback):
        """Context manager exit point."""
        self.close()
        return False  # re-raise any exceptions


def test_MAVCom():
    with MAVCom("udpin:localhost:14445", source_system=111, loglevel=LogLevels.NONE) as client:
        with MAVCom("udpout:localhost:14445", source_system=222, debug=False) as server:
            server.add_component(_BaseComponent(server, mav_type=MAV_TYPE_CAMERA, source_component=22, debug=False))
            client.add_component(_BaseComponent(client, mav_type=MAV_TYPE_GCS, source_component=11, debug=False))

            time.sleep(0.1)

            MAX_PINGS = 4
            client.component[11].send_ping(222, 22, max_pings=MAX_PINGS)
            time.sleep(0.5)

    print(f"{server.source_system = };  {server.message_cnts = }")
    print(f"{client.source_system = };  {client.message_cnts = }")

    test_eq(server.message_cnts[111]['PING'], MAX_PINGS)
    test_eq(server.message_cnts[111]['HEARTBEAT'] > 0, True)
    test_eq(client.message_cnts[222]['PING'], MAX_PINGS)
    test_eq(client.message_cnts[222]['HEARTBEAT'] > 0, True)
