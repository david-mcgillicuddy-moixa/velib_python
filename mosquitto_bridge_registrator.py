import fcntl
import threading
import logging
import os
import requests
import subprocess
import traceback
from ve_utils import exit_on_error

VrmApiServer = 'https://ccgxlogging.victronenergy.com'
CaBundlePath = "/etc/ssl/certs/ccgx-ca.pem"
VrmBroker = 'mqtt.victronenergy.com'
SettingsPath = os.environ.get('DBUS_MQTT_PATH') or '/data/conf/mosquitto.d'
BridgeConfigPath = os.path.join(SettingsPath, 'vrm_bridge.conf')
MqttPasswordFile = "/data/conf/mqtt_password.txt"
BridgeSettings = '''# Generated by MosquittoBridgeRegistrator. Any changes will be overwritten on service start.
connection vrm
address {3}:443
cleansession true
topic N/{0}/# out
topic R/{0}/# in
topic W/{0}/# in
topic P/{0}/in/# in
topic P/{0}/out/# out
remote_clientid {2}
remote_username {5}
remote_password {1}
bridge_cafile {4}
'''
LockFilePath = "/run/mosquittobridgeregistrator.lock"


class RepeatingTimer(threading.Thread):
	def __init__(self, callback, interval):
		threading.Thread.__init__(self)
		self.event = threading.Event()
		self.callback = callback
		self.interval = interval

	def run(self):
		while not self.event.is_set():
			if not self.callback():
				self.event.set()

			# either call your function here,
			# or put the body of the function here
			self.event.wait(self.interval)

	def stop(self):
		self.event.set()


class MosquittoBridgeRegistrator(object):
	"""
	The MosquittoBridgeRegistrator manages a bridge connection between the local Mosquitto
	MQTT server, and the global mqtt.victronenergy.com. It can be called
	concurrently by different processes; efforts will be synchronized using an
	advisory lock file.

	It now also supports registering the API key and getting it and the password without
	restarting Mosquitto. This allows using the API key, but not use the local broker and
	instead connect directly to mqtt.victronenergy.com.
	"""

	def __init__(self, system_id, restart_mosquitto=True):
		self._init_broker_timer = None
		self._aborted = threading.Event()
		self._client_id = None
		self._system_id = system_id
		self._global_broker_username = "ccgxapikey_" + self._system_id
		self._global_broker_password = None
		self._restart_mosquitto = restart_mosquitto
		self._requests_log_level = logging.getLogger("requests").getEffectiveLevel()

	def load_or_generate_mqtt_password(self):
		"""In case posting the password to storemqttpassword.php was processed
		by the server, but we never saw the response, we need to keep it around
		for the next time (don't post a random new one).

		This way of storing the password was incepted later, and makes it
		backwards compatible.
		"""

		if os.path.exists(MqttPasswordFile):
			with open(MqttPasswordFile, "r") as f:
				logging.info("Using {}".format(MqttPasswordFile))
				password = f.read().strip()
				return password
		else:
			with open(MqttPasswordFile, "w") as f:
				logging.info("Writing new {}".format(MqttPasswordFile))
				password = get_random_string(32)
				f.write(password)
				f.flush()
				os.fsync(f.fileno())
				return password

	def register(self):
		if self._init_broker_timer is not None:
			return
		if self._init_broker(quiet=False, timeout=5):
			if not self._aborted.is_set():
				logging.info("[InitBroker] Registration failed. Retrying in thread, silently.")
				logging.getLogger("requests").setLevel(logging.WARNING)
				# Not using gobject to keep these blocking operations out of the event loop
				self._init_broker_timer = RepeatingTimer(self._init_broker, 60)
				self._init_broker_timer.start()

	def abort_gracefully(self):
		self._aborted.set()
		if self._init_broker_timer:
			self._init_broker_timer.stop()
			self._init_broker_timer.join()

	@property
	def client_id(self):
		return self._client_id

	def _init_broker(self, quiet=True, timeout=5):
		try:
			with open(LockFilePath, "a") as lockFile:
				fcntl.flock(lockFile, fcntl.LOCK_EX)

				orig_config = None
				# Read the current config file (if present)
				try:
					if not quiet:
						logging.info('[InitBroker] Reading config file')
					with open(BridgeConfigPath, 'rt') as in_file:
						orig_config = in_file.read()
					settings = dict(tuple(l.strip().split(' ', 1)) for l in orig_config.split('\n')
						if not l.startswith('#') and l.strip() != '')
					self._client_id = settings.get('remote_clientid')
					self._global_broker_password = settings.get('remote_password')
				except IOError:
					if not quiet:
						logging.info('[InitBroker] Reading config file failed.')
				# Fix items missing from config
				if self._client_id is None:
					self._client_id = 'ccgx_' + get_random_string(12)
				if self._global_broker_password is None:
					self._global_broker_password = self.load_or_generate_mqtt_password()
				# Get to the actual registration
				if not quiet:
					logging.info('[InitBroker] Registering CCGX at VRM portal')
				with requests.Session() as session:
					headers = {'content-type': 'application/x-www-form-urlencoded', 'User-Agent': 'dbus-mqtt'}
					r = session.post(
						VrmApiServer + '/log/storemqttpassword.php',
						data=dict(identifier=self._global_broker_username, mqttPassword=self._global_broker_password),
						headers=headers,
						verify=CaBundlePath,
						timeout=(timeout,timeout))
					if r.status_code == requests.codes.ok:
						config = BridgeSettings.format(self._system_id, self._global_broker_password, self._client_id, VrmBroker,
							CaBundlePath, self._global_broker_username)
						# Do we need to adjust the settings file?
						if config != orig_config:
							logging.info('[InitBroker] Writing new config file')
							config_dir = os.path.dirname(BridgeConfigPath)
							if not os.path.exists(config_dir):
								os.makedirs(config_dir)
							with open(BridgeConfigPath, 'wt') as out_file:
								out_file.write(config)
								out_file.flush()
								os.fsync(out_file.fileno())
							self._restart_broker()
						self._init_broker_timer = None
						logging.getLogger("requests").setLevel(self._requests_log_level)
						logging.info('[InitBroker] Registration successful')
						return False
					if not quiet:
						logging.error('VRM registration failed. Http status was: {}'.format(r.status_code))
						logging.error('Message was: {}'.format(r.text))
		except:
			if not quiet:
				traceback.print_exc()
		# Notify the timer we want to be called again
		return True

	def _restart_broker(self):
		if self._restart_mosquitto:
			logging.info('Restarting broker')
			subprocess.call(['svc', '-t', '/service/mosquitto'])

	def get_password(self):
		assert self._global_broker_password is not None
		return self._global_broker_password

	def get_apikey(self):
		return self._global_broker_username


def get_random_string(size=32):
	"""Creates a random (hex) string which contains 'size' characters."""
	return ''.join("{0:02x}".format(ord(b)) for b in open('/dev/urandom', 'rb').read(size/2))

# vim: noexpandtab:shiftwidth=4:tabstop=4:softtabstop=0
