#!/usr/bin/env python3

import pywemo
import time
import logging
import json
import requests
import datetime
from enum import Enum
from dateutil import tz
from time import mktime, sleep
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

    def convert_utc_string_to_local_datetime_obj(self, utc_str):
        time_obj = time.strptime(utc_str, self.DATE_FMT_STRING)
        time_obj = datetime.datetime.fromtimestamp(mktime(time_obj))
        utc = time_obj.replace(tzinfo=self.UTC_ZONE)
        return utc.astimezone(self.LOCAL_ZONE)

    def discover_devices(self):
        # TODO: This finds all WeMo devices, it doesn't filter by which devices are lights or not (maybe I don't care, I only have lights)
        logging.info("Discovering WeMo devices on the local network...")
        t0 = time.time()
        self.devices = pywemo.discover_devices()
        logging.info(f"Found {len(self.devices)} WeMo devices in {time.time() - t0:.2f} seconds.")

    def __init__(self, lat, lng):
        super(LightController, self).__init__()
        self.sunrise_sunset_api_url_base = f'https://api.sunrise-sunset.org/json?lat={lat}&lng={lng}&formatted=0'
        self.set_schedule()

    def get_next_sunrise_sunset_times(self):
        # Specify the local date so that we don't get tomorrow's sunset/sunrise times if it has passed UTC midnight
        today = datetime.datetime.now()
        today_str = f'{today.year}-{today.month}-{today.day}'
        logging.info(f"Today is {today_str}")
        url = f'{self.sunrise_sunset_api_url_base}&date={today_str}'
        sunrise_sunset = requests.get(url)
        sunrise_sunset.raise_for_status()
        sunrise_sunset_data = sunrise_sunset.json()

        if sunrise_sunset_data['status'] != 'OK':
            raise Exception(f"Bad result from Sunrise-Sunset API. (url={url}, result={json.dumps(sunrise_sunset_data, indent=4)})")

        sunrise_time = sunrise_sunset_data['results']['sunrise']
        self.sunrise_time = self.convert_utc_string_to_local_datetime_obj(sunrise_time)
        logging.info(f"Sunrise time: {self.sunrise_time}")

        sunset_time = sunrise_sunset_data['results']['sunset']
        self.sunset_time = self.convert_utc_string_to_local_datetime_obj(sunset_time)
        logging.info(f"Sunset time:  {self.sunset_time}")

    def set_schedule(self):
        self.get_next_sunrise_sunset_times()
        self.schedule = {}

        now = datetime.datetime.now().astimezone(self.LOCAL_ZONE)
        tomorrow = now + datetime.timedelta(days=1)
        time_until_midnight = datetime.datetime.combine(tomorrow, datetime.time.min).astimezone(self.LOCAL_ZONE) - now
        self.schedule[time_until_midnight.total_seconds() + self.POST_MIDNIGHT_BUFFER_SEC] = self.set_schedule

        time_until_sunrise = (self.sunrise_time - now).total_seconds()
        if time_until_sunrise >= 0:
            self.schedule[time_until_sunrise] = self.turn_off_lights
        else:
            logging.info("Sunrise already happened today, not scheduling turn_off_lights until tomorrow.")

        time_until_sunset = (self.sunset_time - now).total_seconds()
        if time_until_sunset >= 0:
            if time_until_sunset >= self.PRE_SUNSET_BUFFER_SECONDS:
                time_until_sunset = time_until_sunset - self.PRE_SUNSET_BUFFER_SECONDS
            self.schedule[time_until_sunset] = self.turn_on_lights
        else:
            logging.info("Sunset already happened today, not scheduling turn_on_lights until tomorrow.")

        logging.info(f"Schedule: {self.schedule}")

    def toggle_lights(self, on_or_off):
        self.discover_devices()

        for d in self.devices:
            if on_or_off == LightStatus.ON:
                cmd = d.on
            elif on_or_off == LightStatus.OFF:
                cmd = d.off
            else:
                raise Exception(f"Unknown LightStatus: {on_or_off}")

            cmd()

            done = True
            while not done:
                if d.state != on_or_off:
                    cmd()
                    sleep(1)
                    done = False

    def turn_on_lights(self):
        self.toggle_lights(LightStatus.ON)

    def turn_off_lights(self):
        self.toggle_lights(LightStatus.OFF)

    def run(self):
        # TODO: Make sure lights are in the correct state based on time of day before starting the schedule loop

        while True:
            schedule = dict(self.schedule)  # Make a copy since one of the tasks updates the schedule
            already_slept = 0
            for time_until_next_task, task_func in sorted(schedule.items()):
                to_sleep = time_until_next_task - already_slept
                logging.info(f"Sleeping {to_sleep:.0f} seconds until running {task_func.__name__}")
                sleep(to_sleep)
                already_slept += to_sleep
                logging.info(f'Running {task_func.__name__}')
                task_func()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    parser = ArgumentParser()
    parser.add_argument('--lat', required=True, type=float, help="Latitude as a floating point number.")
    parser.add_argument('--long', required=True, type=float, help="Longitude as a floating point number.")
    args = parser.parse_args()

    lc = LightController(lat=args.lat, lng=args.long)
    lc.run()


if __name__ == '__main__':
    main()
