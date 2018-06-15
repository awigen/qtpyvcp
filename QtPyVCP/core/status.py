#!/usr/bin/env python

#   Copyright (c) 2018 Kurt Jacobson
#      <kurtcjacobson@gmail.com>
#
#   This file is part of QtPyVCP.
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 2 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.

import linuxcnc, time, threading, subprocess, os, json
from PyQt5.QtCore import QObject, QTimer

# Setup logging
try:
    from QtPyVCP.core import logger
    log = logger.get(__name__)
    log.setLevel('ERROR')
except:
    import logging as log
    FORMAT = "[%(levelname)s]: %(message)s (%(filename)s:%(lineno)d)"
    log.basicConfig(level=log.DEBUG, format=FORMAT)


#==============================================================================
# Status Monitor
#==============================================================================

class StatusItem(object):
    """docstring for StatusItem"""
    def __init__(self, name, index=None, key=None, log_change=False):
        super(StatusItem, self).__init__()
        """StatusItem monitor class

        Args:
            name (str):             the name of the `linuxcnc.status` attribute to monitor
            index (None, optional): the index of the array item to monitor, used with `joint`, `axis` and `tool_table` attributes.
            key (None, optional):   the key of the dict item to monitor, used with `joint`, `axis` and `tool_table` attributes.
            log_change (bool, optional): whether to log value changes of the attribute's value.
        """
        self.name = name
        self.index = index
        self.key = key
        self.log_change = log_change

        self.observers = []
        self.value = None

    def __hash__(self):
        return hash((self.name, self.index, self.key))

    def __eq__(self, other):
        return type(self) == type(other) and (self.name, self.index, self.key) == (other.name, other.index, other.key)

    def __ne__(self, other):
        # Needed to avoid having both x==y and x!=y True at the same time!
        return not(self == other)

    def update(self, status):
        val = getattr(status, self.name)
        if val == self.value:
            return
        if self.index is not None:
            val = val[self.index]
            if self.key is not None:
                val = val[self.key]

        if val != self.value:
            self.value = val

            if self.log_change:
                self.logValue()

            for observer in self.observers:
                if observer is None:
                    continue
                try:
                    observer(self.value)
                except Exception as e:
                    log.exception(e)
                    self.observers.remove(observer)

    def logValue(self):
        index = key = ''
        if self.index is not None:
            index = '[{}]'.format(self.index)
        if self.key is not None:
            key = '[{}]'.format(self.key)
        log.debug("Status value changed: {}{}{} => {}".format(self.name, index, key, self.value))

    def forceUpdate(self, observer=None):
        self.value = None

    def addObserver(self, callback):
        # if callback not in self.observers:
            self.observers.append(callback)

    def removeObserver(self, callback):
        self.observers.remove(callback)


class StatusPoller(QObject):
    """docstring for StatusPoller"""

    stat = linuxcnc.stat()
    timer = QTimer()

    def __init__(self):
        super(StatusPoller, self).__init__()

        self.status_items = {}

        # Start the timer
        self._cycle_time = 75
        self.timer.timeout.connect(self._poll)
        self.timer.start(self._cycle_time)

    def _poll(self):
        # s = time.time()
        try:
            self.stat.poll()
        except Exception as e:
            log.exception(e)
            return
        for status_item in self.status_items.values():
            try:
                status_item.update(self.stat)
            except Exception as e:
                log.exception(e)
                del self.status_items[status_item]
        # print time.time() - s

    def getValue(self, value_name, index=None, key=None):
        """Retrieves the current value of the specified `linuxcnc.stat` attribute.

        Args:
            value_name (str):       the name of a `linuxcnc.stat` attribute
            index (int, optional):  the joint/axis number for array items
            key (str, optional):    the key of the value for dict items

        Returns:
            varies: the value of the specified `linuxcnc.stat` item
        """
        try:
            self.stat.poll() # Poll, otherwise data could be up to `cycle_time` old
            val = getattr(self.stat, self.name)
            if self.index is not None:
                val = val[self.index]
                if self.key is not None:
                    val = val[self.key]
            return val
        except Exception as e:
            log.exception(e)
            return None

    def addObserver(self, attribute, callback, index=None, key=None, log_change=False):
        si = self.status_items.get(hash((attribute, index, key)))
        if si is None:
            log.debug("Adding new StatusItem 'stat.{}'".format(attribute))
            si = StatusItem(attribute, index=index, key=key, log_change=log_change)
            self.status_items[hash(si)] = si
        log.debug("Adding new observer for 'stat.{}': {}".format(attribute, callback))
        si.addObserver(callback)
        si.forceUpdate(callback)

    def removeObserver(self, attribute, callback='', index=None, key=None):
        si = self.status_items.get(hash((attribute, index, key)))
        if si:
            if callback is not '':
                if callback in si.observers:
                    log.debug("Removing '{}' observer: {}".format(attribute, callback))
                    si.removeObserver(callback)
            else:
                del self.status_items[si]
                log.debug("Removing '{}' StatusItem: {}".format(attribute, hash(si)))

class Status(QObject):
    """Ensures only one instance of StatusPoller exists per python interpretor.
    """
    _instance = None
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = StatusPoller()
        return cls._instance


#==============================================================================
# HAL Status Monitor
#==============================================================================

class HALStatusItem(object):
    """docstring for StatusItem"""
    def __init__(self, pin_name, log_change=False):
        super(HALStatusItem, self).__init__()
        """StatusItem monitor class

        Args:
            name (str):                     the name of the HAL pin to monitor
            log_change (bool, optional):    whether to log value changes
        """
        self.pin_name = pin_name
        self.log_change = log_change

        pin_data = subprocess.check_output(['halcmd', '-s', 'show', 'pin', self.pin_name]).split()
        hal_type_map = {'float': float, 's32': int, 'u32': int, 'bit': bool}
        self.type = hal_type_map.get(pin_data[1].strip())

        self.observers = []
        self.value = self.type(pin_data[3])

    def __hash__(self):
        return hash(self.pin_name)

    def __eq__(self, other):
        return type(self) == type(other) and hash(self.pin_name) == hash(other.pin_name)

    def __ne__(self, other):
        # Needed to avoid having both x==y and x!=y True at the same time!
        return not(self == other)

    def update(self, value):
        self.value = self.type(self.value)
        if self.log_change:
            self.logValue()
        for observer in self.observers:
            if observer is None:
                continue
            try:
                observer(self.value)
            except Exception as e:
                log.exception(e)
                self.observers.remove(observer)

    def logValue(self):
        log.debug("HAL value changed: {} => {}".format(self.pin_name, self.value))

    def forceUpdate(self, callback=None):
        data = subprocess.check_output(['halcmd', '-s', 'show', 'pin', self.pin_name]).split()
        value = self.type(data[3])
        if callback is not None:
            callback(value)
        else:
            self.update(value)

    def addObserver(self, callback):
        # if callback not in self.observers:
            self.observers.append(callback)

    def removeObserver(self, callback):
        self.observers.remove(callback)


class HALStatusPoller(QObject):
    """docstring for StatusPoller"""
    def __init__(self):
        super(HALStatusPoller, self).__init__()

        self.cycle_time = 100
        self.linuxcnc_is_alive = False

        self.status_items = {}
        self.pin_dict = {}
        self.sig_dict = {}

        # Create a thread for checking the HAL pins and sigs
        self.hal_mutex = threading.Lock()
        self.hal_thread = threading.Thread(target=self.hal_poll_thread)
        self.hal_thread.daemon = True
        self.hal_thread.start()


    # halcmd can take 200ms or more to run, so run poll updates in a thread so as not to slow the server
    # requests for hal pins and sigs will read the results from the most recent update
    def hal_poll_thread(self):

        while True:
            s = time.time()

            # first, check if linuxcnc is running at all
            if not os.path.isfile( '/tmp/linuxcnc.lock' ):
                self.hal_mutex.acquire()
                try:
                    if self.linuxcnc_is_alive:
                        log.debug("LinuxCNC has stopped.")
                    self.linuxcnc_is_alive = False
                    self.pin_dict = {}
                    self.sig_dict = {}
                finally:
                    self.hal_mutex.release()
                time.sleep(self.cycle_time/1000.0)
                continue
            else:
                if not self.linuxcnc_is_alive:
                    log.debug("LinuxCNC has started.")
                self.linuxcnc_is_alive = True

            self.p = subprocess.Popen( ['halcmd', '-s', 'show', 'pin'] , stderr=subprocess.PIPE, stdout=subprocess.PIPE )
            rawtuple = self.p.communicate()
            if len(rawtuple[0]) <= 0:
                time.sleep(self.cycle_time/1000.0)
                continue
            raw = rawtuple[0].split('\n')

            pins = [ filter( lambda a: a != '', [x.strip() for x in line.split(' ')] ) for line in raw ]

            # UPDATE THE DICTIONARY OF PIN INFO
            # Acquire the mutex so we don't step on other threads
            self.hal_mutex.acquire()
            try:
                pin_dict = {}
                sig_dict = {}

                for p in pins:
                    if len(p) > 5:
                        # if there is a signal listed on this pin, make sure
                        # that signal is in our signal dictionary
                        sig_dict[ p[6] ] = p[3]
                    if len(p) >= 5:
                        pin_dict[ p[4] ] = p[3]
            finally:
                self.hal_mutex.release()

            changed_items = set(pin_dict.items()) - set(self.pin_dict.items())
            # for item in changed_items:
            #     print 'HAL pin Changed: {} => {}'.format(item[0], item[1])

            self.pin_dict = pin_dict
            self.sig_dict = sig_dict

            for changed_item in changed_items:
                if changed_item[0] in self.status_items:
                    self.status_items[changed_item[0]].update(changed_item[1])

            # print time.time() - s
            # print json.dumps(pin_dict, indent=4, sort_keys=True)

            # before starting the next check, sleep a little so we don't use all the CPU
            time.sleep(self.cycle_time/1000.0)

    def addObserver(self, pin_name, callback, log_change=False):
        si = self.status_items.get(pin_name)
        if si is None:
            log.debug("Adding new HALStatusItem '{}'".format(pin_name))
            si = HALStatusItem(pin_name, log_change=log_change)
            self.status_items[pin_name] = si
        log.debug("Adding new observer for HAL '{}': {}".format(pin_name, callback))
        si.addObserver(callback)
        si.forceUpdate(callback)

    def removeObserver(self, pin_name, callback=''):
        si = self.status_items.get(pin_name)
        if si:
            if callback is not '':
                if callback in si.observers:
                    log.debug("Removing '{}' observer: {}".format(pin_name, callback))
                    si.removeObserver(callback)
            else:
                del self.status_items[pin_name]
                log.debug("Removing '{}' HALStatusItem: {}".format(pin_name, si))

class HALStatus(QObject):
    """Ensures only one instance of StatusPoller exists per python interpretor.
    """
    _instance = None
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = HALStatusPoller()
        return cls._instance


#==============================================================================
# For standalone testing
#==============================================================================

if __name__ == '__main__':
    import sys
    from PyQt5.QtWidgets import QWidget, QLabel, QApplication, QVBoxLayout

    app = QApplication(sys.argv)
    dro = QLabel()
    dro.resize(250,50)
    dro.setMargin(30)

    s = Status()
    s.addObserver('position', (lambda v: dro.setNum(v)), index=0, log_change=True)

    h = HALStatus()
    h.addObserver('joint.0.pos-cmd', (lambda v: dro.setNum(v)), log_change=True)

    dro.show()
    sys.exit(app.exec_())
