import json, time
import paho.mqtt.client as mqtt
import requests, threading
from pyfimptoha.light import Light
from pyfimptoha.sensor import Sensor
from pyfimptoha.switch import Switch
from pyfimptoha.mode import Mode

class Client:
    components = {}
    lights = []
    sensors = []
    switches = []

    _devices = [] # discovered fimp devices
    _hassio_token = None
    _ha_host = 'hassio'
    _mqtt = None
    _selected_devices = None
    _topic_discover = "pt:j1/mt:rsp/rt:app/rn:homeassistant/ad:flow1"
    _topic_fimp_event = "pt:j1/mt:evt/rt:dev/rn:zw/ad:1/"
    _topic_ha = "homeassistant/"
    _uptime = 0
    _verbose = False
    _listen_ha = False
    _listen_fimp_event = False

    def __init__(self, mqtt=None, selected_devices=None, token=None, ha_host=None, debug=False):
        self._selected_devices = selected_devices
        self._hassio_token = token
        self._verbose = debug

        if ha_host:
            self._ha_host = ha_host

        if mqtt:
            self._mqtt = mqtt
            mqtt.on_message = self.on_message

        self.start()

        if self._hassio_token:
            threading.Timer(20, self.check_restarts).start()
            # self.check_restarts()


    def log(self, s, verbose = False):
        '''Log'''
        if verbose and not self._verbose:
            return
        print(s)

    def start(self):
        # Add Modus sensor (home, sleep, away and vacation)
        # todo Find a way to auto discover the value. Sensor value is currently
        # empty until changed by the system/user
        self.check_restarts()

        mode = Mode()
        message = mode.get_component()
        self.publish_messages([message])

        # fimp discover
        self.send_fimp_discovery()
        time.sleep(2)
        self.publish_components()
        time.sleep(2)
        # Listen on fimp and homeassistant topics
        self.listen_ha()
        self.listen_fimp()

    def check_restarts(self):
        endpoint_prefix = ''
        if self._ha_host == 'hassio':
            endpoint_prefix = 'homeassistant/'

        sensor_uptime = 'ha_uptime_minutes'
        url = 'http://%s/%sapi/states/sensor.%s' % (self._ha_host, endpoint_prefix, sensor_uptime)
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + self._hassio_token,
        }

        uptime = None
        try:
            r = requests.get(
                url,
                headers=headers
            )

            if r.status_code == 200:
                json = r.json()
                uptime = int(float(json['state']))
                print("Check for HA restart: Current uptime %s minutes" % str(uptime))
            elif r.status_code == 401:
                print("Check for HA restart: Home Assistant Rest API returned 401: Not authorize. Was a long-lived access token set up as mentioned in the README?")
            elif r.status_code == 404:
                print("Check for HA restart: Home Assistant Rest API returned 404: Not found. Sensor `%s` was not found. Was sensor `HA uptime moment` added as mentioned in the README?" % (sensor_uptime))

        except requests.exceptions.RequestException as e:
            print("Check for HA restart: Could not contact HA for uptime details")
            print("Error code: " + r.status_code)
            print(e)
        except:
            print('Something went wrong')


        # if not self._uptime or self._uptime > uptime:
        if uptime == None:
            print("check_restarts: Could not get uptime from HA. Trying again in 60 seconds")
            threading.Timer(60, self.check_restarts).start()
            return

        if self._uptime == 0:
            self._uptime = uptime

        if self._uptime > uptime:
            print("check_restarts: HA has restarted. Doing auto discovery")
            self._uptime = uptime
            self.start()

        # print('check_restarts: Uptime end', self._uptime)

        threading.Timer(60, self.check_restarts).start()

    # The callback for when a PUBLISH message is received from the server.
    def on_message(self, client, userdata, msg):
        payload = str(msg.payload.decode("utf-8"))

        # Discover FIMP devices and create HA components out of them
        if msg.topic == self._topic_discover:
            data = json.loads(payload)
            self.create_components(data["val"]["param"]["device"])

        # Received commands from HA
        elif msg.topic.startswith(self._topic_ha):
            self.process_ha(msg.topic, payload)

        # Received event from FIMP - Update HA state_topic
        # pt:j1/mt:evt/rt:dev/rn:zw/ad:1/
        elif msg.topic.startswith(self._topic_fimp_event):
            # print('Got fimp event !')
            messages = self.process_fimp_event(msg.topic, payload)
            self.publish_messages(messages)

    def publish_messages(self, messages):
        """Publish list of messages over MQTT"""
        if self._verbose:
            print('Publish messages', messages)

        if self._mqtt and messages:
            for data in messages:
                self._mqtt.publish(data["topic"], data["payload"])

    def send_fimp_discovery(self):
        """Load FIMP devices from MQTT broker"""

        path = "pyfimptoha/data/fimp_discover.json"
        topic = "pt:j1/mt:cmd/rt:app/rn:vinculum/ad:1"
        with open(path) as json_file:
            data = json.load(json_file)

            # Subscribe to : pt:j1/mt:rsp/rt:app/rn:homeassistant/ad:flow1
            self._mqtt.subscribe(self._topic_discover)
            message = {
                'topic': topic,
                'payload': json.dumps(data)
            }
            self.log('Asking FIMP to expose all devices...')
            self.publish_messages([message])

    def publish_components(self):
        """
        Publish components and their initial states
        """

        # - Lights
        # todo Add support for Wall plugs with functionality 'Lighting'
        for unique_id, component in self.components.items():
            message = component.get_component()
            self.publish_messages([message])

        # Publish init states
        print("Publishing init states")
        print("- lights")
        time.sleep(0.5)
        for light in self.lights:
            init_state = light.get_state()
            for data in init_state:
                self.publish_messages([data])
                time.sleep(0.1)

        print("- switches")
        time.sleep(0.5)
        for switch in self.switches:
            init_state = switch.get_init_state()
            self.publish_messages(init_state)
            time.sleep(0.1)

        print("- sensors")
        time.sleep(0.5)
        for sensor in self.sensors:
            init_state = sensor.get_init_state()
            self.publish_messages(init_state)
            time.sleep(0.1)

    def load_json_device(self, filename):
        data = "{}"

        path = "pyfimptoha/data/%s" % filename
        with open(path) as json_file:
            data = json.load(json_file)
        self._devices.append(data)

    def create_components(self, devices):
        """ Creates HA components out of FIMP devices"""
        self._devices = devices
        self.log('Received list of devices from FIMP. FIMP reported %s devices' % (len(devices)))
        self.log('Devices without rooms are ignored')

        for device in self._devices:
            address = device["fimp"]["address"]
            name = device["client"]["name"]
            functionality = device["functionality"]
            room = device["room"]

            # Skip device without room
            if device["room"] == None:
                # self.log('Skipping: %s %s' % (address, name))
                continue

            #  When debugging: Ignore everything except self._selected_devices if set
            if self._selected_devices and address not in self._selected_devices:
                self.log('Skipping: %s %s' % (address, name))
                continue

            self.log("Creating: %s %s" % (address, name))
            self.log("- Functionality: %s" % (functionality))

            for service_name in device["services"]:
                component = None
                component_address = address + "-" + service_name
                service = device["services"][service_name]

                if (
                    functionality == "lighting" and (
                        service_name.startswith("out_bin_switch") or
                        service_name.startswith("out_lvl_switch")
                    )
                ):
                    component = "light"
                elif functionality == "appliance" and (
                    service_name.startswith("out_bin_switch")
                ):
                    component = "switch"
                elif service_name in Sensor.supported_services():
                    component = "sensor"

                if not component:
                    self.log("- Skipping %s. Not yet supported" % service_name, True)
                    continue

                self.log("- Creating component %s - %s" % (component, service_name), True)

                # todo Add support for binary_sensor
                if component == "sensor":
                    sensor = Sensor(service_name, service, device)
                    self.sensors.append(sensor)
                    self.components[sensor.unique_id] = sensor
                elif component == "switch":
                    switch = Switch(service_name, service, device)
                    self.switches.append(switch)
                    self.components[switch.unique_id] = switch
                elif component == "light":
                    light = Light(service_name, service, device)
                    self.lights.append(light)
                    self.components[light.unique_id] = light

    def process_ha(self, topic, payload):
        """
        Process a message from ha and generates one to FIMP

        Eg: 
        - Topic: homeassistant/light/7-out_lvl_switch/set
        - Payload: ON

        Or set dimmer level
        - Topic: homeassistant/light/7-out_lvl_switch/command
        - Payload: 33
        """

        topic = topic.split("/")
        result = None

        # Topic like: homeassistant/light/7-out_lvl_switch/command
        if len(topic) > 3:
            # print("process_ha:topic", topic)
            component = topic[1]
            unique_id = topic[2]
            topic_type = topic[3]

            if component == "light":
                for light in self.lights:
                    if light.unique_id == unique_id:
                        messages = light.handle_ha(topic_type, payload)

                        if self._verbose:
                            print("process ha: messages", messages)
                        self.publish_messages(messages)

        return

    def process_fimp_event(self, topic, payload):
        """
        Process a message from FIMP and generates one to HA

        Eg: Dimmer update
        - Topic: pt:j1/mt:evt/rt:dev/rn:zw/ad:1/sv:out_lvl_switch/ad:7_1

        """
        topic = topic.split("/")
        payload = json.loads(payload)

        if len(topic) > 6:
            tmp, type = topic[1].split(":", 1) # evt or cmd
            tmp, service_name = topic[5].split(":", 1) # out_lvl_switch, sensor_power, etc
            tmp, address = topic[6].split(":", 1)
            address, tmp = address.split("_", 1) # device address: 1 or higher
            unique_id = address + "-" + service_name

            # debug Exclude all but light 7
            # if address != "7":
            #     return None

            component = self.components.get(unique_id)
            if not component:
                return None

            data = component.handle_fimp(payload)
            return data

        return None

    def listen_ha(self):
        """
        Enables listening on HA topics
        """
        self._listen_ha = True
        self._mqtt.subscribe(self._topic_ha + "#")

    def listen_fimp(self):
        """
        Enables listening on FIMP event topics
        """
        self._listen_fimp_event = True
        self._mqtt.subscribe(self._topic_fimp_event + "#")

