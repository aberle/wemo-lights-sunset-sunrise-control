#!/usr/bin/env python3

import os
import sys
import time
import json
import pywemo
import logging
import colorlog
import requests
import datetime
import traceback
import threading

from enum import Enum
from dateutil import tz
from argparse import ArgumentParser
from time import mktime, sleep, strftime, gmtime


class LightStatus(Enum):
    OFF = 0
    ON = 1


class LightController(object):
    UTC_ZONE = tz.tzutc()
    LOCAL_ZONE = tz.tzlocal()
    DATE_FMT_STRING = '%Y-%m-%dT%H:%M:%S+00:00'
    RELAUNCH_SLEEP_SEC = 10

    DEFAULT_CONFIG = {
        'POST_MIDNIGHT_BUFFER_SEC': 15,
        'PRE_SUNSET_BUFFER_MINUTES': 30,
        'LIGHT_RESPONSE_TIMEOUT_SEC': 10,
        'LIGHT_RESPONSE_BACKOFF_SEC': 0.25,
        'HTTP_REQUEST_TIMEOUT_SEC': 10,
        'DISCOVER_DEVICES_ATTEMPTS': 5
    }

    def __init__(self, lat, lng):
        super(LightController, self).__init__()

        # Read config from a file and fill in the blanks with DEFAULT_CONFIG
        self.config = self.DEFAULT_CONFIG
        if os.path.exists('config.json'):
            with open('config.json') as f:
                self.config.update(json.load(f))
        self.control_map = self.config.get('CONTROL_MAP', {})
        logging.info(json.dumps(self.config, indent=4))

        self.did_ignore_first_sub_event = {d: False for d in self.control_map}
        self.sub = pywemo.SubscriptionRegistry()
        logging.info("Starting SubscriptionRegistry HTTP server")
        self.sub.start()
        self.registered_master_devices = []
        self.sunrise_sunset_api_url_base = f'http://api.sunrise-sunset.org/json?lat={lat}&lng={lng}&formatted=0'
        self.set_schedule()

        # Make sure that if we crashed while trying to turn on/off lights we don't come back in a bad state
        self.discover_devices()
        now = self.now()
        if now > self.sunset_time:
            logging.info("It is after sunset, turning all lights on.")
            self.turn_on_lights(do_discover=False)
        elif now > self.sunrise_time:
            logging.info("It is after sunrise, turning all lights off.")
            self.turn_off_lights(do_discover=False)

    def convert_utc_string_to_local_datetime_obj(self, utc_str):
        """
        Converts the API result UTC strings to a local datetime.datetime
        """
        time_obj = time.strptime(utc_str, self.DATE_FMT_STRING)
        time_obj = datetime.datetime.fromtimestamp(mktime(time_obj))
        utc = time_obj.replace(tzinfo=self.UTC_ZONE)
        return utc.astimezone(self.LOCAL_ZONE)

    def master_light_cb(self, device, typ, param):
        """
        Callback that is run when a master light reports a BINARY_STATE event. Turns on or off all
        dependent lights in self.control_list depending on what the event reports.
        In case we have a problem (e.g. the IP address of the lights changes), try to rediscover them
        """
        logging.info(f"{'Ignoring the first' if not self.did_ignore_first_sub_event[device.name] else 'Got a' } {typ}:{param} event from device {device.name}")

        if not self.did_ignore_first_sub_event[device.name]:
            # Ignore the first event reporting the state of the light so that it does
            # not contradict the first command sent after device discovery
            self.did_ignore_first_sub_event[device.name] = True
            return

        try:
            if LightStatus(int(param)) == LightStatus.OFF:
                self.turn_off_lights(do_discover=False, only=self.control_map[device.name])
            else:
                self.turn_on_lights(do_discover=False, only=self.control_map[device.name])
        except Exception:
            logging.error(f"Encountered a problem toggling lights, rediscovering. traceback={traceback.format_exc()}")
            self.discover_devices()

    def discover_devices(self):
        """
        This finds all WeMo devices on the network. It will also register all master
        devices in self.control_map to toggle their assigned dependent devices accordingly.
        """
        logging.info("Discovering WeMo devices on the local network...")
        t0 = time.time()

        # Unregister all devices from the SubscriptionRegistry in case they have changed
        for d in self.registered_master_devices:
            self.sub.unregister(d)
        self.registered_master_devices = []

        attempts = 1
        self.devices = {d.name: d for d in pywemo.discover_devices()}
        num_devices_expected = len(self.control_map.keys()) + sum([len(i) for i in self.control_map.values()])

        while len(self.devices) != num_devices_expected and attempts < self.config['DISCOVER_DEVICES_ATTEMPTS']:
            logging.error(f"Found {len(self.devices)} devices, but expected to find {num_devices_expected}. Discovered devices: {self.devices.keys()}")
            self.devices = {d.name: d for d in pywemo.discover_devices()}
            attempts += 1

        if attempts == self.config['DISCOVER_DEVICES_ATTEMPTS']:
            logging.error(f"Gave up discovring devices after {self.config['DISCOVER_DEVICES_ATTEMPTS']} attempts.")

        for dname, d in self.devices.items():
            if dname in self.control_map:
                self.did_ignore_first_sub_event[dname] = False
                d.ensure_long_press_virtual_device()
                self.sub.register(d)
                self.registered_master_devices.append(d)
                self.sub.on(d, pywemo.subscribe.EVENT_TYPE_BINARY_STATE, self.master_light_cb)

        logging.info(f"Found {len(self.devices)} WeMo devices in {time.time() - t0:.2f} seconds.")
        logging.info(f"Registered {len(self.registered_master_devices)} master devices for controlling other lights.")

    def now(self):
        """
        Returns a datetime.datetime for this exact moment in the local timezone
        """
        return datetime.datetime.now().astimezone(self.LOCAL_ZONE)

    def get_next_sunrise_sunset_times(self):
        """
        Sets self.sunset_time and self.sunrise_time for the current sunset and sunrise
        times for today. This may be in the past if either already happened today.
        """
        today = datetime.datetime.now()
        today_str = f'{today.year}-{today.month}-{today.day}'
        logging.info(f"Today is {today_str}")
        url = f'{self.sunrise_sunset_api_url_base}&date={today_str}'
        sunrise_sunset = requests.get(url, timeout=self.config['HTTP_REQUEST_TIMEOUT_SEC'])
        sunrise_sunset.raise_for_status()
        sunrise_sunset_data = sunrise_sunset.json()

        if sunrise_sunset_data['status'] != 'OK':
            raise Exception(f"Bad result from Sunrise-Sunset API. (url={url}, result={json.dumps(sunrise_sunset_data, indent=4)})")

        sunrise_time = sunrise_sunset_data['results']['sunrise']
        self.sunrise_time = self.convert_utc_string_to_local_datetime_obj(sunrise_time)
        logging.info(f"Sunrise time: {self.sunrise_time}")

        # Turn the lights on PRE_SUNSET_BUFFER_MINUTES before actual sunset
        sunset_time = sunrise_sunset_data['results']['sunset']
        self.sunset_time = self.convert_utc_string_to_local_datetime_obj(sunset_time) - datetime.timedelta(minutes=self.config['PRE_SUNSET_BUFFER_MINUTES'])
        logging.info(f"Sunset time:  {self.sunset_time}")

    def set_schedule(self):
        """
        Sets self.schedule as a dictionary of {local datetime.datetime: function_to_run}.
        The run() function iterates over this schedule in order of which task should be executed

        Always schedules itself at POST_MIDNIGHT_BUFFER_SEC after midnight.
        Does not schedule either turn on/off lights if sunset/sunrise have already happened, respectively.
        """
        self.get_next_sunrise_sunset_times()
        self.schedule = {}

        now = self.now()
        tomorrow = now + datetime.timedelta(days=1)
        midnight = datetime.datetime.combine(tomorrow, datetime.time.min).astimezone(self.LOCAL_ZONE) + datetime.timedelta(seconds=self.config['POST_MIDNIGHT_BUFFER_SEC'])
        self.schedule[midnight] = self.set_schedule

        if now < self.sunrise_time:
            self.schedule[self.sunrise_time] = self.turn_off_lights
        else:
            logging.info(f"Sunrise already happened today, not scheduling turn_off_lights until {midnight}.")

        if now < self.sunset_time:
            self.schedule[self.sunset_time] = self.turn_on_lights
        else:
            logging.info(f"Sunset already happened today, not scheduling turn_on_lights until {midnight}.")

        logging.info("Schedule:")
        for time_scheduled, task_func in sorted(self.schedule.items()):
            logging.info(f"  * {time_scheduled}: {task_func.__name__}")

    def run_light_command(self, device, on_or_off):
        if on_or_off == LightStatus.ON:
            cmd = device.on
        elif on_or_off == LightStatus.OFF:
            cmd = device.off
        else:
            raise Exception(f"Unknown LightStatus: {on_or_off}")

        cmd()

        t0 = time.time()
        done = False
        while not done:
            if device.get_state() != on_or_off:
                if time.time() - t0 > self.config['LIGHT_RESPONSE_TIMEOUT_SEC']:
                    raise Exception(f"Failed to turn {on_or_off.name} device {device.name} after {self.config['LIGHT_RESPONSE_TIMEOUT_SEC']} seconds.")
                sleep(self.config['LIGHT_RESPONSE_BACKOFF_SEC'])
                cmd()
                done = True

    def set_light_state(self, on_or_off, do_discover=True, only=None):
        """
        Discovers all WeMo light devices on the network and either turns
        them all on or all off, depending on the parameter.

        Raises an exception if the device does not end up in the
        correct state after LIGHT_RESPONSE_TIMEOUT_SEC seconds.
        """
        if do_discover:
            self.discover_devices()

        devices = {}
        if only is not None:
            for dname in only:
                devices[dname] = self.devices[dname]
        else:
            devices = self.devices

        logging.info(f"Turning {on_or_off.name} {len(devices)} lights: {[d.name for d in devices.values()]}")

        threads = []

        for d in devices.values():
            t = threading.Thread(target=self.run_light_command, args=(d, on_or_off))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

    def turn_on_lights(self, do_discover=True, only=None):
        """
        Convenience function to call set_light_state(ON)
        """
        self.set_light_state(LightStatus.ON, do_discover, only)

    def turn_off_lights(self, do_discover=True, only=None):
        """
        Convenience function to call set_light_state(OFF)
        """
        self.set_light_state(LightStatus.OFF, do_discover, only)

    def run(self):
        """
        Loops over self.schedule forever, in order, with the closest upcoming task first.
        Sleeps until it is time to execute the task and then calls the function.
        """
        while True:
            schedule = dict(self.schedule)  # Make a copy since one of the tasks updates the schedule
            for time_scheduled, task_func in sorted(schedule.items()):
                now = self.now()
                time_until_task = (time_scheduled - now).total_seconds()
                logging.info(f"Sleeping {strftime('%H hours, %M minutes, and %S seconds', gmtime(time_until_task))} until running {task_func.__name__}.")
                sleep(time_until_task)
                logging.info(f'Running {task_func.__name__}')
                task_func()


def main():
    # Configure logging and set pywemo logging to WARN level only since it's a little noisy
    formatter = colorlog.ColoredFormatter('%(thin_white)s%(asctime)s%(reset)s [%(log_color)s%(levelname)s%(reset)s] %(message)s',)
    root = logging.getLogger()
    log_handler = logging.StreamHandler(sys.stdout)
    root.setLevel(logging.INFO)
    log_handler.setFormatter(formatter)
    root.addHandler(log_handler)
    logging.getLogger('pywemo').setLevel(logging.WARNING)

    parser = ArgumentParser()
    parser.add_argument('--lat', required=True, type=float, help="Latitude as a floating point number.")
    parser.add_argument('--long', required=True, type=float, help="Longitude as a floating point number.")
    args = parser.parse_args()

    # Create the LightController and run forever. If there's an unhandled exception sleep a bit and restart
    while True:
        try:
            lc = LightController(lat=args.lat, lng=args.long)
            lc.run()
        except Exception:
            logging.error(f"Unhandled exception\n{traceback.format_exc()}")
            logging.error(f"Sleeping {LightController.RELAUNCH_SLEEP_SEC} seconds and running again.")
            sleep(LightController.RELAUNCH_SLEEP_SEC)


if __name__ == '__main__':
    main()
