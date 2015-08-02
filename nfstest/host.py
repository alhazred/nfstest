#===============================================================================
# Copyright 2012 NetApp, Inc. All Rights Reserved,
# contribution by Jorge Mora <mora@netapp.com>
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#===============================================================================
"""
Host module

Provides a set of tools for running commands on the local host or a remote
host, including a mechanism for running commands in the background.
It provides methods for mounting and unmounting from an NFS server and
a mechanism to simulate a network partition via the use of 'iptables'.
Currently, there is no mechanism to restore the iptables rules to their
original state.
"""
import os
import re
import time
import ctypes
import socket
import subprocess
import nfstest_config as c
from baseobj import BaseObj

# Module constants
__author__    = 'Jorge Mora (%s)' % c.NFSTEST_AUTHOR_EMAIL
__version__   = '1.0.5'
__copyright__ = "Copyright (C) 2012 NetApp, Inc."
__license__   = "GPL v2"

class Host(BaseObj):
    """Host object

       Host() -> New Host object

       Usage:
           from nfstest.host import Host

           # Create host object for local host
           x = Host()
           # Create host object for remote host
           y = Host(host='192.168.0.11')

           # Run command to the local host
           x.run_cmd("ls -l")

           # Send command to the remote host and run it as root
           y.run_cmd("ls -l", sudo=True)

           # Run command in the background
           x.run_cmd("tcpdump", sudo=True, wait=False)
           ....
           ....
           # Stop command running in the background
           x.stop_cmd()

           # Mount volume using default options
           x.mount()

           # Unmount volume
           x.umount()
    """
    def __init__(self, **kwargs):
        """Constructor

           Initialize object's private data.

           host:
               Hostname or IP address [default: localhost]
           user:
               User to log in to host [default: '']
           server:
               NFS server name or IP address [default: None]
           nfsversion:
               NFS version [default: 4]
           minorversion:
               NFS minor version [default: 1]
           proto:
               NFS protocol name [default: 'tcp']
           port:
               NFS server port [default: 2049]
           sec:
               Security flavor [default: 'sys']
           export:
               Exported file system to mount [default: '/']
           mtpoint:
               Mount point [default: '/mnt/t']
           datadir:
               Data directory where files are created [default: '']
           mtopts:
               Mount options [default: 'hard,rsize=4096,wsize=4096']
           interface:
               Network device interface [default: 'eth0']
           nomount:
               Debug option so the server is not actually mounted [default: False]
           iptables:
               Iptables command [default: '/usr/sbin/iptables']
           sudo:
               Sudo command [default: '/usr/bin/sudo']
        """
        # Arguments
        self.host         = kwargs.pop("host",         '')
        self.user         = kwargs.pop("user",         '')
        self.server       = kwargs.pop("server",       '')
        self.nfsversion   = kwargs.pop("nfsversion",   c.NFSTEST_NFSVERSION)
        self.minorversion = kwargs.pop("minorversion", c.NFSTEST_MINORVERSION)
        self.proto        = kwargs.pop("proto",        c.NFSTEST_NFSPROTO)
        self.port         = kwargs.pop("port",         c.NFSTEST_NFSPORT)
        self.sec          = kwargs.pop("sec",          c.NFSTEST_NFSSEC)
        self.export       = kwargs.pop("export",       c.NFSTEST_EXPORT)
        self.mtpoint      = kwargs.pop("mtpoint",      c.NFSTEST_MTPOINT)
        self.datadir      = kwargs.pop("datadir",      '')
        self.mtopts       = kwargs.pop("mtopts",       c.NFSTEST_MTOPTS)
        self.interface    = kwargs.pop("interface",    c.NFSTEST_INTERFACE)
        self.nomount      = kwargs.pop("nomount",      False)
        self.iptables     = kwargs.pop("iptables",     c.NFSTEST_IPTABLES)
        self.sudo         = kwargs.pop("sudo",         c.NFSTEST_SUDO)
        # Initialize object variables
        self.mtdir = self.mtpoint
        self.mounted = False
        self.process_list = []
        self.process_smap = {}
        self.process_dmap = {}
        self._checkmtpoint = []
        self._invalidmtpoint = []
        self._mtpoint_created = []
        self.need_network_reset = False
        self._localhost = False if len(self.host) > 0 else True
        self.fqdn = socket.getfqdn(self.host)
        ipv6 = self.proto[-1] == '6'
        self.ipaddr = self.get_ip_address(host=self.host, ipv6=ipv6)

        # Load share library - used for functions not implemented
        # in the python modules "os" or "posix"
        from ctypes.util import find_library
        self.libc = ctypes.CDLL(find_library('c'))

    def __del__(self):
        """Destructor

           Gracefully unmount volume and reset network.
        """
        if self.need_network_reset:
            self.network_reset()
        if self.mounted:
            self.umount()
        for mtpoint in self._mtpoint_created:
            try:
                cmd = "rmdir %s" % mtpoint
                self.run_cmd(cmd, sudo=True, dlevel='DBG3', msg="Removing mount point directory: ")
            except:
                pass

    def sudo_cmd(self, cmd):
        """Prefix the SUDO command if effective user is not root."""
        if os.getuid() != 0:
            # Not root -- prefix sudo command
            cmd = self.sudo + ' ' + cmd
        return cmd

    def run_cmd(self, cmd, sudo=False, dlevel='DBG1', msg='', wait=True):
        """Run the command to the remote machine using ssh.
           There is no user authentication, so remote host must allow
           ssh connection without any passwords for the user.
           For a localhost the command is just executed and ssh is not used.

           The object for the process of the command is stored in object
           attribute 'self.process' to be used by methods wait_cmd() and
           stop_cmd(). The standard output of the command is also stored
           in the object attribute 'self.pstdout' while the standard error
           output of the command is stored in 'self.pstderr'.

           cmd:
               Command to execute
           sudo:
               Run command using sudo if option is True
           dlevel:
               Debug level for displaying the command to the user
           msg:
               Prefix this message to the debug message to be displayed
           wait:
               Wait for command to complete before returning

           Return the standard output of the command and the return code or
           exit status is stored in the object attribute 'self.returncode'.
        """
        self.process = None
        self.pstdout = '' 
        self.pstderr = '' 
        self.perror  = '' 
        self.returncode = 0
        if self.user is not None and len(self.user) > 0:
            user = self.user + '@'
        else:
            user = ''

        # Add sudo command if specified
        if sudo:
            cmd = self.sudo_cmd(cmd)

        if not self._localhost:
            cmd = 'ssh -t -t %s%s "%s"' % (user, self.host, cmd.replace('"', '\\"'))

        self.dprint(dlevel, msg + cmd)
        self.process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not wait:
            self.process_list.append(self.process)
            self.process_dmap[self.process.pid] = dlevel
            if sudo:
                self.process_smap[self.process.pid] = 1
            return
        self.pstdout, self.pstderr = self.process.communicate()
        self.process.wait()
        self.returncode = self.process.returncode
        if self._localhost:
            if self.process.returncode:
                # Error on local command
                self.perror = self.pstderr
                raise Exception(self.pstderr)
        else:
            if self.process.returncode == 255:
                # Error on ssh command
                raise Exception(self.pstderr)
            elif self.process.returncode:
                # Error on command sent
                self.perror = self.pstdout
                raise Exception(self.pstdout)
        return self.pstdout

    def wait_cmd(self, process=None, terminate=False, dlevel=None, msg=''):
        """Wait for command started by run_cmd() to finish.

           process:
               The object for the process of the command to wait for,
               or wait for all commands started by run_cmd() if this
               option is not given
           terminate:
               If True, send a signal to terminate the command or commands
               and then wait for all commands to finish
           dlevel:
               Debug level for displaying the command to the user, default
               is the level given by run_cmd()
           msg:
               Prefix this message to the debug message to be displayed

           Return the exit status of the last command
        """
        if process is None:
            plist = list(self.process_list)
        else:
            plist = [process]

        out = None
        for proc in plist:
            if proc in self.process_list:
                if dlevel is None:
                    _dlevel = self.process_dmap.get(proc.pid, None)
                    if _dlevel is not None:
                        dlevel = _dlevel
                if terminate:
                    if proc.pid in self.process_smap:
                        # This process was started with sudo so kill it with
                        # sudo since terminate() fails to actually kill the
                        # command in RHEL or python 2.6
                        self.run_cmd("kill %d" % proc.pid, sudo=True, dlevel=dlevel, msg=msg)
                        self.process_smap.pop(proc.pid)
                    else:
                        self.dprint(dlevel, msg + "stopping process %d" % proc.pid)
                        proc.terminate()
                else:
                    self.dprint(dlevel, msg + "waiting for process %d" % proc.pid)
                out = proc.wait()
                self.process_list.remove(proc)
        return out

    def stop_cmd(self, process=None, dlevel=None, msg=''):
        """Terminate command started by run_cmd() by calling wait_cmd()
           with the 'terminate' option set to True.

           process:
               The object for the process of the command to terminate,
               or terminate all commands started by run_cmd() if this
               option is not given
           dlevel:
               Debug level for displaying the command to the user, default
               is the level given by run_cmd()
           msg:
               Prefix this message to the debug message to be displayed

           Return the exit status of the last command
        """
        out = self.wait_cmd(process, terminate=True, dlevel=dlevel, msg=msg)
        return out

    def _check_mtpoint(self, mtpoint):
        """Check if mount point exists."""
        if mtpoint in self._checkmtpoint:
            # Run this method once per mount point
            return
        isdir = True
        self._checkmtpoint.append(mtpoint)
        if self._localhost:
            # Locally check if mount point exists and is a directory
            exist = os.path.exists(mtpoint)
            if exist:
                isdir = os.path.isdir(mtpoint)
        else:
            # Remotely check if mount point exists and is a directory
            try:
                cmd = "test -e '%s'" % mtpoint
                self.run_cmd(cmd, dlevel='DBG4', msg="Check if mount point directory exists: ")
            except:
                pass
            exist = not self.returncode
            if exist:
                try:
                    cmd = "test -d '%s'" % mtpoint
                    self.run_cmd(cmd, dlevel='DBG4', msg="Check if mount point is a directory: ")
                except:
                    pass
                isdir = not self.returncode
        if not exist:
            cmd = "mkdir -p %s" % mtpoint
            self.run_cmd(cmd, sudo=True, dlevel='DBG3', msg="Creating mount point directory: ")
            self._mtpoint_created.append(mtpoint)
        elif not isdir:
            self._invalidmtpoint.append(mtpoint)
            raise Exception("Mount point %s is not a directory" % mtpoint)

    def mount(self, **kwargs):
        """Mount the file system on the given mount point.

           server:
               NFS server name or IP address [default: self.server]
           nfsversion:
               NFS version [default: self.nfsversion]
           minorversion:
               NFS minor version [default: self.minorversion]
           proto:
               NFS protocol name [default: self.proto]
           port:
               NFS server port [default: self.port]
           sec:
               Security flavor [default: self.sec]
           export:
               Exported file system to mount [default: self.export]
           mtpoint:
               Mount point [default: self.mtpoint]
           datadir:
               Data directory where files are created [default: self.datadir]
           mtopts:
               Mount options [default: self.mtopts]

           Return the mount point.
        """
        # Get options
        server       = kwargs.pop("server",       self.server)
        nfsversion   = kwargs.pop("nfsversion",   self.nfsversion)
        minorversion = kwargs.pop("minorversion", self.minorversion)
        proto        = kwargs.pop("proto",        self.proto)
        port         = kwargs.pop("port",         self.port)
        sec          = kwargs.pop("sec",          self.sec)
        export       = kwargs.pop("export",       self.export)
        mtpoint      = kwargs.pop("mtpoint",      self.mtpoint)
        datadir      = kwargs.pop("datadir",      self.datadir)
        mtopts       = kwargs.pop("mtopts",       self.mtopts)

        # Remove trailing '/' on mount point
        mtpoint = mtpoint.rstrip("/")

        if len(datadir):
            self.mtdir = os.path.join(mtpoint, datadir)
        else:
            self.mtdir = mtpoint

        self._check_mtpoint(mtpoint)
        if self.nomount or mtpoint in self._invalidmtpoint:
            return

        if len(export) > 1:
            # Remove trailing '/' on export path if is not the root directory
            export = export.rstrip("/")

        if mtopts[-1] != ',':
            mtopts += ','

        # Using the proper version of NFS
        minorversion_str = ""
        if nfsversion == 4:
            minorversion_str = "minorversion=%d," % minorversion

        # Mount command
        cmd = "mount -o vers=%d,%s%sproto=%s,sec=%s,port=%d %s:%s %s" % (nfsversion, minorversion_str, mtopts, proto, sec, port, server, export, mtpoint)
        self.run_cmd(cmd, sudo=True, dlevel='DBG2', msg="Mount volume: ")

        self.mounted = True
        self.mtpoint = mtpoint

        # Create data directory if it does not exist
        if self._localhost:
            if not os.path.exists(self.mtdir):
                os.mkdir(self.mtdir, 0777)
        else:
            try:
                cmd = "test -e '%s'" % self.mtdir
                self.run_cmd(cmd, dlevel='DBG4', msg="Check if data directory exists: ")
            except:
                pass
            if self.returncode:
                cmd = "mkdir -m 0777 -p %s" % self.mtdir
                self.run_cmd(cmd, dlevel='DBG3', msg="Creating data directory: ")

        # Return the mount point
        return mtpoint

    def umount(self):
        """Unmount the file system."""
        if self.nomount:
            return

        self._check_mtpoint(self.mtpoint)
        if self.mtpoint in self._invalidmtpoint:
            return

        self.dprint('DBG3', "Sync all buffers to disk")
        self.libc.sync()

        # Try to umount 5 times
        cmd = "umount -f %s" % self.mtpoint
        for i in range(5):
            time.sleep(1)
            try:
                self.run_cmd(cmd, sudo=True, dlevel='DBG2', msg="Unmount volume: ")
            except:
                pass

            if self.returncode == 0 or re.search('not (mounted|found)', self.perror):
                # Unmount succeeded or directory not mounted
                self.mounted = False
                break
            self.dprint('DBG2', self.perror)

    def network_drop(self, ipaddr, port):
        """Simulate a network drop by dropping all tcp packets going to the
           given ipaddr and port using the iptables commands.
        """
        self.need_network_reset = True
        cmd = "%s -A OUTPUT -p tcp -d %s --dport %d -j DROP" % (self.iptables, ipaddr, port)
        self.run_cmd(cmd, sudo=True, dlevel='DBG6', msg="Network drop: ")

    def network_reset(self):
        """Reset the network by flushing all the chains in the table using
           the iptables command.
        """
        try:
            cmd = "%s --flush" % self.iptables
            self.run_cmd(cmd, sudo=True, dlevel='DBG6', msg="Network reset: ")
        except:
            self.dprint('DBG6', "Network reset error <%s>" % self.perror)

        try:
            cmd = "%s --delete-chain" % self.iptables
            self.run_cmd(cmd, sudo=True, dlevel='DBG6', msg="Network reset: ")
        except:
            self.dprint('DBG6', "Network reset error <%s>" % self.perror)

    def get_route(self, ipaddr):
        """Get routing information for destination IP address
           Returns a tuple: (gateway, device name, src IP address)
        """
        try:
            cmd = "ip route get %s" % ipaddr
            out = self.run_cmd(cmd, dlevel='DBG1', msg="Get routing info: ")
            regex = re.search(r"(\svia\s+(\S+))?\sdev\s+(\S+).*\ssrc\s+(\S+)", out)
            if regex:
                return regex.groups()[1:]
        except:
            self.dprint('DBG7', self.perror)
        return (None, None, None)

    @staticmethod
    def get_ip_address(host='', ipv6=False):
        """Get IP address associated with the given host name.
           This could be run as an instance or class method.
        """
        lstr = ""
        ipstr = "v6" if ipv6 else "v4"
        family = socket.AF_INET6 if ipv6 else socket.AF_INET
        if len(host) == 0:
            lstr = "local "
            host = socket.gethostname()

        try:
            infolist = socket.getaddrinfo(host, 2049, 0, 0, socket.SOL_TCP)
        except Exception as e:
            infolist = []
        for info in infolist:
            # Ignore loopback addresses
            if info[0] == family and info[4][0] not in ('127.0.0.1', '::1'):
                return info[4][0]
        raise Exception("Unable to get IP%s address for %shost '%s'" % (ipstr, lstr, host))
