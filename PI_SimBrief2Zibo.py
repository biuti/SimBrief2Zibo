"""
SimBrief2Zibo
X-Plane plugin

Copyright (c) 2023, Antonio Golfari
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree. 
"""

import json

from pathlib import Path
from urllib import request
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta


from XPPython3 import xp


# Version
__VERSION__ = 'v0.1.beta'

# Plugin parameters required from XPPython3
plugin_name = 'SimBrief2Zibo'
plugin_sig = 'xppython3.simbrief2zibo'
plugin_desc = 'Fetches latest OFP Data from SimBrief and creates the file ZIBO B738 requires'
loop_schedule = 30  # positive numbers are seconds, 0 disabled, negative numbers are cycles


class PythonInterface:

    def __init__(self) -> None:
        self.plugin_name = f"{plugin_name} - {__VERSION__}"
        self.plugin_sig = plugin_sig
        self.plugin_desc = plugin_desc

        # folders init
        self.xp_root = Path(xp.getSystemPath())
        self.prefs = Path(xp.getPrefsPath()).parent
        self.plans = Path(self.prefs.parent, 'FMS plans')

        # Dref init
        self.speed = xp.findDataRef('sim/flightmodel/position/groundspeed')
        self.gears_on_ground = xp.findDataRef('sim/flightmodel2/gear/on_ground')
        self.engines_burning_fuel = xp.findDataRef('sim/flightmodel2/engines/engine_is_burning_fuel')

        xp.log(f"root: {self.xp_root} | prefs: {self.prefs} {self.prefs.is_dir()} | plans: {self.plans} {self.plans.is_dir()}")

        # app init
        self.config_file = Path(self.prefs, 'simbrief2zibo.prf')
        self.pilot_id = None  # SimBrief UserID, int
        self.ofp_id = None  # OFP generated ID
        self.url = None  # SimBrief API url
        self.origin = None  # OFP departure ICAO
        self.destination = None  # OFP destination ICAO
        self.fp_filename = None  # fms/fmx filename
        self.fp_link = None  # link to download XP12 fms from SimBrief

        self.flight_started = False  # tracks simulation phase
        self.fp_checked = False  # tracks app phase

        # load settings
        self.load_settings()

    @property
    def simbrief_url(self) -> str | bool:
        return f"https://www.simbrief.com/api/xml.fetcher.php?userid={self.pilot_id}&json=1"

    @property
    def engines_started(self) -> bool:
        values = []
        xp.getDatavi(self.engines_burning_fuel, values, count=2)
        return any(values)

    @property
    def on_ground(self) -> bool:
        values = []
        xp.getDatavi(self.gears_on_ground, values, count=3)
        return all(values)

    @property
    def at_gate(self) -> bool:
        return self.on_ground and not self.engines_started

    def loopCallback(self, lastCall, elapsedTime, counter, refCon):
        """Loop Callback"""
        _, acf_path = xp.getNthAircraftModel(0)
        if 'B737-800X' in acf_path and not self.flight_started and self.pilot_id:
            xp.log(f' - {datetime.now().strftime("%H:%M:%S")} Loop started...')
            xp.log(f"FP checked: {self.fp_checked} | At gate: {self.at_gate} | Flight started: {self.flight_started}")
            if not self.fp_checked and self.at_gate:
                # check fp
                xp.log(f"starting FP routine...")
                self.check_simbrief()
            elif not self.flight_started and not self.at_gate:
                # flight mode, do nothing
                xp.log(f'set flight started...')
                xp.scheduleFlightLoop(self.loop_id, loop_schedule*10)
                self.flight_started = True
            elif self.flight_started and self.at_gate:
                # look for a new OFP for a turnaround flight
                xp.log(f'set flight ended...')
                xp.scheduleFlightLoop(self.loop_id, loop_schedule)
                self.flight_started = False
                self.fp_checked = False
        return loop_schedule

    def load_settings(self) -> bool:
        if self.config_file.is_file():
            # read file
            with open(self.config_file, 'r') as f:
                data = f.read()
            # parse file
            settings = json.loads(data)
            self.pilot_id = settings.get('pilot_id')
            return True
        else:
            #! user id until a widget is created
            #! self.pilot_id = 
            #! return True
            pass

    def check_simbrief(self):
        xp.log(f"pilotID = {self.pilot_id}, contacting SimBrief...")
        ofp = self.read_ofp()
        if ofp.get('error'):
            # some error occurred
            xp.log(f"check_simbrief: {ofp.get('error')}")
            return

        if self.ofp_id and self.ofp_id == ofp.get('params').get('request_id'):
            # no new OFP
            return

        self.origin = ofp.get('origin').get('icao_code')
        self.destination = ofp.get('destination').get('icao_code')
        self.fp_link = ofp.get('fms_downloads').get('directory') + ofp.get('fms_downloads').get('xpe').get('link')
        xp.log(f"ORIGIN: {self.origin} | DESTINATION: {self.destination} | link: {self.fp_link}")
        if self.origin and self.destination:
            self.get_fp_filename()
            data = self.parse_ofp(ofp)
            xp.log(f"fp filename: {self.fp_filename} | data: {data}")
            if self.create_xml_file(data):
                self.fp_checked = True


    def read_ofp(self) -> json:
        try:
            response = request.urlopen(self.simbrief_url)
            ofp = json.loads(response.read())
            return ofp
        except HTTPError | URLError as e:
            return {'error': 'Error retrieving OFP: {e}'}

    def parse_ofp(self, ofp: json) -> dict:
        """
        LIDO: \n400 288/021 -54  400 320/020 -54  400 332/028 -55  350 330/022 -44\n380 272/019 -50  380 310/016 -50  380 333/024 -51  310 343/025 -34\n360 285/017 -46  360 319/017 -46  360 331/023 -46  200 005/009 -10\n340 301/016 -42  340 326/019 -42  340 328/021 -42  150 297/004 +02\n320 313/019 -36  320 328/021 -37  320 332/024 -37  100 258/001 +11
        """

        data = {}
        fix = ofp.get('navlog').get('fix')[-1]
        if fix.get('ident') == self.destination:
            dest_isa = int(fix.get('oat_isa_dev'))
        else:
            # use avg isa dev
            dest_isa = int(ofp.get('general').get('avg_temp_dev'))
        
        dest_metar = ofp.get('destination').get('metar')
        
        data['dest_isa'] = dest_isa
        data['dest_metar'] = dest_metar
        text = ofp.get('text').get('plan_html').split('DESCENT')[1].split('\n\n')[0]
        lines = text.split('\n')[1:]
        data['winds'] = [tuple(l.split()[-3:]) for l in lines]
        return data

    def get_fp_filename(self):
        yesterday = datetime.now() - timedelta(days=1)
        files = [
            f for f in self.plans.iterdir()
            if f.suffix in ('.fms', '.fmx')
            and datetime.fromtimestamp(f.stat().st_ctime) > yesterday
            and f.stem.startswith(self.origin)
            and self.destination in f.stem
        ]
        if files:
            # user already created a FP for this OFP
            file = max(files, key=lambda x: x.stat().st_ctime)
            self.fp_filename = file.stem
        else:
            # we need to download FP from SimBrief
            self.download_fp()

    def download_fp(self):
        fp_filename = self.origin + self.destination + '.fms'
        file = Path(self.plans, fp_filename)
        try:
            result = request.urlretrieve(self.fp_link, file)
            self.fp_filename = file.stem
        except HTTPError | URLError as e:
            pass

    def create_xml_file(self, data: dict) -> bool:
        """we need to recreate the plan_html parts we use as in LIDO format"""

        # const
        summary_tag = '''<div style="line-height:14px;font-size:13px"><pre><!--BKMK///OFP///0--><!--BKMK///Summary and Fuel///1--><b>[ OFP ]\n--------------------------------------------------------------------</b>\nOFP 1\n\n'''
        wind_tag = '''<h2 style="page-break-after: always;"> </h2><!--BKMK///Wind Information///1-->--------------------------------------------------------------------\n WIND INFORMATION \nDESCENT\n'''
        wx_tag = '''<h2 style="page-break-after: always;"> </h2><!--BKMK///Airport WX List///0--><b>[ Airport WX List ]\n--------------------------------------------------------------------</b>\nDestination:\n'''

        dest_isa = f"AVG ISA       {'M' if data['dest_isa'] < 0 else 'P'}{abs(data['dest_isa']):03d}\n\n"
        winds = '\n'.join([' '.join([e for e in el]) for el in data['winds']]) + '\n\n'
        parts = data['dest_metar'].split()[1:]
        parts[0] = parts[0].replace('Z', ' ')
        dest_metar = f"{self.destination}\nSA  {' '.join(parts)}\n"

        root = ET.Element("OFP")
        text = ET.SubElement(root, "text")
        plan_html = ET.SubElement(text, "plan_html")
        plan_html.text = summary_tag + dest_isa + wind_tag + winds + wx_tag + dest_metar
        filename = self.fp_filename + '.xml'
        file = Path(self.plans, filename)
        tree = ET.ElementTree(root)
        try:
            tree.write(file)
            return True
        except Exception as e:
            xp.log(f"Error writing xml file: {e}")
            return False

    def XPluginStart(self):
        # loopCallback
        self.loop = self.loopCallback
        self.loop_id = xp.createFlightLoop(self.loop, 0)
        xp.log(f"flightloop created, ID {self.loop_id}")
        xp.scheduleFlightLoop(self.loop_id, loop_schedule)
        return self.plugin_name, self.plugin_sig, self.plugin_desc

    def XPluginEnable(self):
        return 1

    def XPluginStop(self):
        # Called once by X-Plane on quit (or when plugins are exiting as part of reload)
        xp.log(f"flightloop destroyed ...")
        xp.destroyFlightLoop(self.loop_id)
        pass
