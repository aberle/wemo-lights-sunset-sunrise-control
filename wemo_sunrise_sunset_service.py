#!/usr/bin/env python3

import pywemo
import time
import logging
import json
import requests
import datetime
import traceback
from enum import Enum
from dateutil import tz
from time import mktime, sleep, strftime, gmtime
from argparse import ArgumentParser


class LightStatus(Enum):
    OFF = 0
    ON = 1


class LightController(object):
    UTC_ZONE = tz.tzutc()
    LOCAL_ZONE = tz.tzlocal()
    DATE_FMT_STRING = '%Y-%m-%dT%H:%M:%S+00:00'
    POST_MIDNIGHT_BUFFER_SEC = 10
    PRE_SUNSET_BUFFER_SECONDS = 30 * 60  # Turn the lights on this many seconds before actual sunset time
    LIGHT_DEVICE_TYPES = ['LightSwitch', 'Switch', 'Dimmer']
    LIGHT_RESPONSE_TIMEOUT_SEC = 10
    LIGHT_RESPONSE_BACKOFF_SEC = 0.25
    HTTP_REQUEST_TIMEOUT_SEC = 10
    RELAUNCH_SLEEP_SEC = 10

    def __init__(self, lat, lng):
        super(LightController, self).__init__()
        self.sunrise_sunset_api_url_base = f'https://api.sunrise-sunset.org/json?lat={lat}&lng={lng}&formatted=0'
        self.set_schedule()

        # Make sure that if we crashed while trying to turn on/off lights we don't come back in a bad state
        now = self.now()
        if now > self.sunset_time:
            self.turn_on_lights()
        elif now > self.sunrise_time:
            self.turn_off_lights()

    def convert_utc_string_to_local_datetime_obj(self, utc_str):
        """
        Converts the API result UTC strings to a local datetime.datetime
        """
        time_obj = time.strptime(utc_str, self.DATE_FMT_STRING)
        time_obj = datetime.datetime.fromtimestamp(mktime(time_obj))
        utc = time_obj.replace(tzinfo=self.UTC_ZONE)
        return utc.astimezone(self.LOCAL_ZONE)

    def discover_devices(self):
        """
        This finds all WeMo devices that are in the self.LIGHT_DEVICE_TYPES list
        """
        logging.info("Discovering WeMo devices on the local network...")
        t0 = time.time()
        self.devices = [d for d in pywemo.discover_devices() if d.device_type in self.LIGHT_DEVICE_TYPES]
        logging.info(f"Found {len(self.devices)} WeMo devices in {time.time() - t0:.2f} seconds.")

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
        sunrise_sunset = requests.get(url, timeout=self.HTTP_REQUEST_TIMEOUT_SEC)
        sunrise_sunset.raise_for_status()
        sunrise_sunset_data = sunrise_sunset.json()

        if sunrise_sunset_data['status'] != 'OK':
            raise Exception(f"Bad result from Sunrise-Sunset API. (url={url}, result={json.dumps(sunrise_sunset_data, indent=4)})")

        sunrise_time = sunrise_sunset_data['results']['sunrise']
        self.sunrise_time = self.convert_utc_string_to_local_datetime_obj(sunrise_time)
        logging.info(f"Sunrise time: {self.sunrise_time}")

        sunset_time = sunrise_sunset_data['results']['sunset']
        self.sunset_time = self.convert_utc_string_to_local_datetime_obj(sunset_time) - datetime.timedelta(seconds=self.PRE_SUNSET_BUFFER_SECONDS)
        logging.info(f"Sunset time:  {self.sunset_time}")

    def set_schedule(self):
        """
        Sets self.schedule as a dictionary of {local datetime.datetime: function_to_run}.
        The run() function iterates over this schedule in order of which task should be executed

        Always schedules itself at 10 seconds after midnight.
        Does not schedule either turn on/off lights if sunset/sunrise have already happened, respectively.
        """
        self.get_next_sunrise_sunset_times()
        self.schedule = {}

        now = self.now()
        tomorrow = now + datetime.timedelta(days=1)
        midnight = datetime.datetime.combine(tomorrow, datetime.time.min).astimezone(self.LOCAL_ZONE) + datetime.timedelta(seconds=self.POST_MIDNIGHT_BUFFER_SEC)
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

    def toggle_lights(self, on_or_off):
        """
        Discovers all WeMo light devices on the network and either turns
        them all on or all off, depending on the parameter.

        Raises an exception if the device does not end up in the
        correct state after self.LIGHT_RESPONSE_TIMEOUT_SEC seconds.
        """
        self.discover_devices()
        logging.info(f"Turning {on_or_off.name} {len(self.devices)} lights.")

        for d in self.devices:
            if on_or_off == LightStatus.ON:
                cmd = d.on
            elif on_or_off == LightStatus.OFF:
                cmd = d.off
            else:
                raise Exception(f"Unknown LightStatus: {on_or_off}")

            cmd()

            t0 = time.time()
            done = False
            while not done:
                if d.get_state() != on_or_off:
                    if time.time() - t0 > self.LIGHT_RESPONSE_TIMEOUT_SEC:
                        raise Exception(f"Failed to turn {on_or_off.name} device {d.name} after {self.LIGHT_RESPONSE_TIMEOUT_SEC} seconds.")
                    sleep(self.LIGHT_RESPONSE_BACKOFF_SEC)
                    cmd()
                    done = True

    def turn_on_lights(self):
        """
        Convenience function to call toggle_lights(ON)
        """
        self.toggle_lights(LightStatus.ON)

    def turn_off_lights(self):
        """
        Convenience function to call toggle_lights(OFF)
        """
        self.toggle_lights(LightStatus.OFF)

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
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

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