from flask.ext.principal import identity_changed, Identity
from tornado.web import StaticFileHandler, HTTPError
from flask import url_for, make_response, request, current_app
from flask.ext.login import login_required, login_user
from werkzeug.utils import redirect
from sockjs.tornado import SockJSConnection

import datetime
import stat
import mimetypes
import email
import time
import os
import threading
import logging
from functools import wraps

from octoprint.settings import settings
import octoprint.timelapse
import octoprint.server
from octoprint.users import ApiUser

def restricted_access(func, apiEnabled=True):
	"""
	If you decorate a view with this, it will ensure that first setup has been
	done for OctoPrint's Access Control plus that any conditions of the
	login_required decorator are met. It also allows to login using the masterkey or any
	of the user's apikeys if API access is enabled globally and for the decorated view.

	If OctoPrint's Access Control has not been setup yet (indicated by the "firstRun"
	flag from the settings being set to True and the userManager not indicating
	that it's user database has been customized from default), the decorator
	will cause a HTTP 403 status code to be returned by the decorated resource.

	If an API key is provided and it matches a known key, the user will be logged in and
	the view will be called directly. If the provided key doesn't match any known key,
	a HTTP 403 status code will be returned by the decorated resource.

	Otherwise the result of calling login_required will be returned.
	"""
	@wraps(func)
	def decorated_view(*args, **kwargs):
		# if OctoPrint hasn't been set up yet, abort
		if settings().getBoolean(["server", "firstRun"]) and (octoprint.server.userManager is None or not octoprint.server.userManager.hasBeenCustomized()):
			return make_response("OctoPrint isn't setup yet", 403)

		# if API is globally enabled, enabled for this request and an api key is provided, try to use that
		if settings().get(["api", "enabled"]) and apiEnabled and "apikey" in request.values.keys():
			apikey = request.values["apikey"]
			user = None

			if apikey == settings().get(["api", "key"]):
				# master key was used
				user = ApiUser()
			else:
				# user key might have been used
				user = octoprint.server.userManager.findUser(apikey=apikey)

			if user is None:
				make_response("Invalid API key", 403)
			if login_user(user, remember=False):
				identity_changed.send(current_app._get_current_object(), identity=Identity(user.get_id()))
				return func(*args, **kwargs)

		# call regular login_required decorator
		return login_required(func)(*args, **kwargs)
	return decorated_view


def api_access(func):
	@wraps(func)
	def decorated_view(*args, **kwargs):
		if not settings().get(["api", "enabled"]):
			make_response("API disabled", 401)
		if not "apikey" in request.values.keys():
			make_response("No API key provided", 401)
		if request.values["apikey"] != settings().get(["api", "key"]):
			make_response("Invalid API key", 403)
		return func(args, kwargs)
	return decorated_view

#~~ Printer state


class PrinterStateConnection(SockJSConnection):
	def __init__(self, printer, gcodeManager, userManager, eventManager, session):
		SockJSConnection.__init__(self, session)

		self._logger = logging.getLogger(__name__)

		self._temperatureBacklog = []
		self._temperatureBacklogMutex = threading.Lock()
		self._logBacklog = []
		self._logBacklogMutex = threading.Lock()
		self._messageBacklog = []
		self._messageBacklogMutex = threading.Lock()

		self._printer = printer
		self._gcodeManager = gcodeManager
		self._userManager = userManager
		self._eventManager = eventManager

	def _getRemoteAddress(self, info):
		forwardedFor = info.headers.get("X-Forwarded-For")
		if forwardedFor is not None:
			return forwardedFor.split(",")[0]
		return info.ip

	def on_open(self, info):
		self._logger.info("New connection from client: %s" % self._getRemoteAddress(info))
		self._printer.registerCallback(self)
		self._gcodeManager.registerCallback(self)
		octoprint.timelapse.registerCallback(self)

		self._eventManager.fire("ClientOpened")
		self._eventManager.subscribe("MovieDone", self._onMovieDone)
		self._eventManager.subscribe("SlicingStarted", self._onSlicingStarted)
		self._eventManager.subscribe("SlicingDone", self._onSlicingDone)
		self._eventManager.subscribe("SlicingFailed", self._onSlicingFailed)

		octoprint.timelapse.notifyCallbacks(octoprint.timelapse.current)

	def on_close(self):
		self._logger.info("Closed client connection")
		self._printer.unregisterCallback(self)
		self._gcodeManager.unregisterCallback(self)
		octoprint.timelapse.unregisterCallback(self)

		self._eventManager.fire("ClientClosed")
		self._eventManager.unsubscribe("MovieDone", self._onMovieDone)
		self._eventManager.unsubscribe("SlicingStarted", self._onSlicingStarted)
		self._eventManager.unsubscribe("SlicingDone", self._onSlicingDone)
		self._eventManager.unsubscribe("SlicingFailed", self._onSlicingFailed)

	def on_message(self, message):
		pass

	def sendCurrentData(self, data):
		# add current temperature, log and message backlogs to sent data
		with self._temperatureBacklogMutex:
			temperatures = self._temperatureBacklog
			self._temperatureBacklog = []

		with self._logBacklogMutex:
			logs = self._logBacklog
			self._logBacklog = []

		with self._messageBacklogMutex:
			messages = self._messageBacklog
			self._messageBacklog = []

		data.update({
		"temperatures": temperatures,
		"logs": logs,
		"messages": messages
		})
		self._emit("current", data)

	def sendHistoryData(self, data):
		self._emit("history", data)

	def sendUpdateTrigger(self, type, payload=None):
		self._emit("updateTrigger", {"type": type, "payload": payload})

	def sendFeedbackCommandOutput(self, name, output):
		self._emit("feedbackCommandOutput", {"name": name, "output": output})

	def sendTimelapseConfig(self, timelapseConfig):
		self._emit("timelapse", timelapseConfig)

	def addLog(self, data):
		with self._logBacklogMutex:
			self._logBacklog.append(data)

	def addMessage(self, data):
		with self._messageBacklogMutex:
			self._messageBacklog.append(data)

	def addTemperature(self, data):
		with self._temperatureBacklogMutex:
			self._temperatureBacklog.append(data)

	def _onMovieDone(self, event, payload):
		self.sendUpdateTrigger("timelapseFiles")

	def _onSlicingStarted(self, event, payload):
		self.sendUpdateTrigger("slicingStarted", payload)

	def _onSlicingDone(self, event, payload):
		self.sendUpdateTrigger("slicingDone", payload)

	def _onSlicingFailed(self, event, payload):
		self.sendUpdateTrigger("slicingFailed", payload)

	def _emit(self, type, payload):
		self.send({type: payload})


#~~ customized large response handler


class LargeResponseHandler(StaticFileHandler):

	CHUNK_SIZE = 16 * 1024

	def initialize(self, path, default_filename=None, as_attachment=False):
		StaticFileHandler.initialize(self, path, default_filename)
		self._as_attachment = as_attachment

	def get(self, path, include_body=True):
		path = self.parse_url_path(path)
		abspath = os.path.abspath(os.path.join(self.root, path))
		# os.path.abspath strips a trailing /
		# it needs to be temporarily added back for requests to root/
		if not (abspath + os.path.sep).startswith(self.root):
			raise HTTPError(403, "%s is not in root static directory", path)
		if os.path.isdir(abspath) and self.default_filename is not None:
			# need to look at the request.path here for when path is empty
			# but there is some prefix to the path that was already
			# trimmed by the routing
			if not self.request.path.endswith("/"):
				self.redirect(self.request.path + "/")
				return
			abspath = os.path.join(abspath, self.default_filename)
		if not os.path.exists(abspath):
			raise HTTPError(404)
		if not os.path.isfile(abspath):
			raise HTTPError(403, "%s is not a file", path)

		stat_result = os.stat(abspath)
		modified = datetime.datetime.fromtimestamp(stat_result[stat.ST_MTIME])

		self.set_header("Last-Modified", modified)

		mime_type, encoding = mimetypes.guess_type(abspath)
		if mime_type:
			self.set_header("Content-Type", mime_type)

		cache_time = self.get_cache_time(path, modified, mime_type)

		if cache_time > 0:
			self.set_header("Expires", datetime.datetime.utcnow() +
									   datetime.timedelta(seconds=cache_time))
			self.set_header("Cache-Control", "max-age=" + str(cache_time))

		self.set_extra_headers(path)

		# Check the If-Modified-Since, and don't send the result if the
		# content has not been modified
		ims_value = self.request.headers.get("If-Modified-Since")
		if ims_value is not None:
			date_tuple = email.utils.parsedate(ims_value)
			if_since = datetime.datetime.fromtimestamp(time.mktime(date_tuple))
			if if_since >= modified:
				self.set_status(304)
				return

		if not include_body:
			assert self.request.method == "HEAD"
			self.set_header("Content-Length", stat_result[stat.ST_SIZE])
		else:
			with open(abspath, "rb") as file:
				while True:
					data = file.read(LargeResponseHandler.CHUNK_SIZE)
					if not data:
						break
					self.write(data)
					self.flush()

	def set_extra_headers(self, path):
		if self._as_attachment:
			self.set_header("Content-Disposition", "attachment")


#~~ reverse proxy compatible wsgi middleware


class ReverseProxied(object):
	"""
	Wrap the application in this middleware and configure the
	front-end server to add these headers, to let you quietly bind
	this to a URL other than / and to an HTTP scheme that is
	different than what is used locally.

	In nginx:
		location /myprefix {
			proxy_pass http://192.168.0.1:5001;
			proxy_set_header Host $host;
			proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
			proxy_set_header X-Scheme $scheme;
			proxy_set_header X-Script-Name /myprefix;
		}

	Alternatively define prefix and scheme via config.yaml:
		server:
			baseUrl: /myprefix
			scheme: http

	:param app: the WSGI application
	"""

	def __init__(self, app):
		self.app = app

	def __call__(self, environ, start_response):
		script_name = environ.get('HTTP_X_SCRIPT_NAME', '')
		if not script_name:
			script_name = settings().get(["server", "baseUrl"])

		if script_name:
			environ['SCRIPT_NAME'] = script_name
			path_info = environ['PATH_INFO']
			if path_info.startswith(script_name):
				environ['PATH_INFO'] = path_info[len(script_name):]

		scheme = environ.get('HTTP_X_SCHEME', '')
		if not scheme:
			scheme = settings().get(["server", "scheme"])

		if scheme:
			environ['wsgi.url_scheme'] = scheme
		return self.app(environ, start_response)


def redirectToTornado(request, target):
	requestUrl = request.url
	appBaseUrl = requestUrl[:requestUrl.find(url_for("index") + "/ajax")]

	redirectUrl = appBaseUrl + target
	if "?" in requestUrl:
		fragment = requestUrl[requestUrl.rfind("?"):]
		redirectUrl += fragment
	return redirect(redirectUrl)

