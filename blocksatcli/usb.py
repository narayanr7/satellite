"""Linux USB demodulatos"""
from pprint import pformat
from ipaddress import IPv4Interface
import os, sys, signal, argparse, subprocess, time, logging, threading, json
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
from . import config, util, defs, rp, firewall
import textwrap
from shutil import which
logger = logging.getLogger(__name__)


def _setup_logfile(cfg_dir):
    """Setup directory and file for dvbv5-zap logs"""
    log_dir = os.path.join(cfg_dir, "logs")
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    name    = "usb-" + time.strftime("%Y%m%d-%H%M%S") + ".log"
    logfile = os.path.join(log_dir, name)
    return logfile


def _find_v4l_lnb(info):
    """Find suitable LNB within v4l-utils preset LNBs

    The LNBs on list v4l_lnbs are defined at lib/libdvbv5/dvb-sat.c

    """


    target_lo_freq_raw  = info['lnb']['lo_freq']
    target_is_universal = info['lnb']['universal']

    if (target_is_universal):
        assert(isinstance(target_lo_freq_raw, list))
        assert(len(target_lo_freq_raw) == 2)
        target_lo_freq = [int(x) for x in target_lo_freq_raw]
    else:
        target_lo_freq = [int(target_lo_freq_raw)]

    # Find options that match the LO freq
    options = list()
    for lnb in defs.v4l_lnbs:
        is_universal_option = 'highfreq' in lnb

        if (target_is_universal and (not is_universal_option)):
            continue

        if (info['lnb']['pol'].lower() == "dual" and # user has dual-pol LNB
            (info['sat']['pol'] == "H" or # and satellite requires H pol
             (info['lnb']['v1_pointed'] and # or LNB already operates with H pol
              info['lnb']['v1_psu_voltage'] >= 16)
            ) and "rangeswitch" not in lnb): # but LNB candidate is single-pol
            continue # not a validate candidate, skip

        if (target_is_universal):
            if (is_universal_option and
                lnb['lowfreq'] == target_lo_freq[0] and
                lnb['highfreq'] == target_lo_freq[1]):
                options.append(lnb)
        else:
            if (lnb['lowfreq'] == target_lo_freq[0]):
                options.append(lnb)

    assert(len(options) > 0), "LNB doesn't match a valid option"
    logger.debug("Matching LNB options: {}".format(pformat(options)))

    return options[0]


def _check_debian_net_interfaces_d(is_root):
    """Check if /etc/network/interfaces.d/ is included as source-directory

    If the distribution doesn't have the `/etc/network/interface` directory,
    just return.

    """
    if_file  = "/etc/network/interfaces"

    if (not os.path.exists(if_file)):
        return

    if_dir   = "/etc/network/interfaces.d/"
    src_line = "source /etc/network/interfaces.d/*"

    if (not is_root):
        print(textwrap.fill("Make sure directory "
                            "\"{}\" "
                            "is considered as source for network "
                            "interface configurations. "
                            "Check if your \"{}\" file "
                            "contains the following line:".format(
                                if_dir, if_file
                            )))
        print("\n{}\n".format(src_line))
        print("If not, add it.\n")
        return

    with open(if_file) as fd:
        current_cfg = fd.read()

    src_included = False
    for line in current_cfg.splitlines():
        if src_line in line:
            src_included = True
            break

    if (src_included):
        return

    with open(if_file, 'a') as fd:
        fd.write("\n" + src_line)


def _add_to_netplan(ifname, addr_with_prefix, gateway, is_root):
    """Create configuration file at /etc/netplan/"""
    assert("/" in addr_with_prefix)
    cfg_dir = "/etc/netplan/"

    cfg = ("network:\n"
           "  version: 2\n"
           "  renderer: networkd\n"
           "  ethernets:\n"
           "    {}:\n"
           "      dhcp4: no\n"
           "      optional: true\n"
           "      addresses: [{}]\n"
           "      gateway4: {}\n").format(
               ifname, addr_with_prefix, gateway)

    fname  = "blocksat-" + ifname + ".yaml"
    path   = os.path.join(cfg_dir, fname)

    if (not is_root):
        print(textwrap.fill("Create a file named {} at {} and add the "
                            "following to it:".format(fname, cfg_dir)))
        print("\n" + cfg + "\n")
        return

    with open(path, 'w') as fd:
        fd.write(cfg)

    print("Created configuration file {} in {}".format(fname, cfg_dir))


def _add_to_interfaces_d(ifname, addr, netmask, gateway, is_root):
    """Create configuration file at /etc/network/interfaces.d/"""
    if_dir = "/etc/network/interfaces.d/"
    cfg = ("iface {0} inet static\n"
           "    address {1}\n"
           "    netmask {2}\n").format(ifname, addr, netmask, gateway)
    fname  = ifname + ".conf"
    path   = os.path.join(if_dir, fname)

    if (not is_root):
        print(textwrap.fill("Create a file named {} at {} and add the "
                            "following to it:".format(fname, if_dir)))
        print("\n" + cfg + "\n")
        return

    with open(path, 'w') as fd:
        fd.write(cfg)

    print("Created configuration file {} in {}".format(fname, if_dir))


def _add_to_sysconfig_net_scripts(ifname, addr, netmask, gateway, is_root):
    """Create configuration file at /etc/sysconfig/network-scripts/"""
    cfg_dir = "/etc/sysconfig/network-scripts/"
    cfg = ("NM_CONTROLLED=\"yes\"\n"
           "DEVICE=\"{}\"\n"
           "BOOTPROTO=none\n"
           "ONBOOT=\"no\"\n"
           "IPADDR={}\n"
           "NETMASK={}\n").format(ifname, addr, netmask, gateway)

    fname  = "ifcfg-" + ifname
    path   = os.path.join(cfg_dir, fname)

    if (not is_root):
        print(textwrap.fill("Create a file named {} at {} and add the "
                            "following to it:".format(fname, cfg_dir)))
        print("\n" + cfg + "\n")
        return

    with open(path, 'w') as fd:
        fd.write(cfg)

    print("Created configuration file {} in {}".format(fname, cfg_dir))


def _set_static_iface_ip(ifname, ipv4_if, is_root):
    """Set static IP address for target network interface

    Args:
        ifname  : Network device name
        ipv4_if : IPv4Interface object with the target interface address
        is_root : (bool) whether running as root

    """
    isinstance(ipv4_if, IPv4Interface)
    addr                = str(ipv4_if.ip)
    addr_with_prefix    = ipv4_if.with_prefixlen
    netmask             = str(ipv4_if.netmask)
    addr_s              = addr.split(".")
    assert(addr_s[-1] != "1")
    gateway_s           = addr_s
    gateway_s[-1]       = "1"
    gateway             = ".".join(gateway_s)


    if (which("netplan") is not None):
        _add_to_netplan(ifname, addr_with_prefix, gateway, is_root)
    elif (os.path.exists("/etc/network/interfaces.d/")):
        _add_to_interfaces_d(ifname, addr, netmask, gateway, is_root)
    elif (os.path.exists("/etc/sysconfig/network-scripts")):
        _add_to_sysconfig_net_scripts(ifname, addr, netmask, gateway, is_root)
    else:
        raise ValueError("Could not set a static IP address on interface "
                         "{}".format(ifname))


def _check_ip(net_if, ip_addr):
    """Check if interface has IP and if it matches target IP

    Args:
        net_if  : DVB network interface name
        ip_addr : Target IP address for the DVB interface slash subnet mask

    Returns:
        (Bool, Bool) Tuple of booleans. The first indicates whether interface
        already has an IP. The second indicates whether the interface IP (if
        existing) matches with respect to a target IP.

    """
    try:
        res = subprocess.check_output(["ip", "addr", "show", "dev", net_if],
                                      stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        return False, False

    has_ip = False
    ip_ok  = False
    for line in res.splitlines():
        if "inet" in line.decode() and "inet6" not in line.decode():
            has_ip    = True
            # Check if IP matches target
            inet_info = line.decode().split()
            inet_if   = IPv4Interface(inet_info[1])
            target_if = IPv4Interface(ip_addr)
            ip_ok     = (inet_if == target_if)
            break

    return has_ip, ip_ok


def _set_ip(net_if, ip_addr, verbose):
    """Set the IP of the DVB network interface

    Args:
        net_if    : DVB network interface name
        ip_addr   : Target IP address for the DVB interface slash subnet mask
        verbose   : Controls verbosity

    """
    is_root       = (os.geteuid() == 0)
    has_ip, ip_ok = _check_ip(net_if, ip_addr)
    inet_if       = IPv4Interface(ip_addr)

    if (has_ip and not ip_ok):
        if (is_root):
            print("Interface %s has an IP, but it is not %s" %(net_if, ip_addr))
            print("Flush current IP address of %s" %(net_if))
        res = util.run_or_print_root_cmd(["ip", "address", "flush",
                                          "dev", net_if], logger)

    if (not has_ip or not ip_ok):
        if (is_root):
            print("Assign static IP address %s to %s" %(ip_addr, net_if))
        else:
            print("Flush the IP from {}:\n".format(net_if))

        res = util.run_or_print_root_cmd(["ip", "address", "flush",
                                          "dev", net_if], logger)

        if (not is_root):
            print()

        _set_static_iface_ip(net_if, inet_if, is_root)
    else:
        if (verbose):
            print("%s already has IP %s" %(net_if, ip_addr))


def _set_ips(net_ifs, ip_addrs, verbose=True):
    """Set IPs of one or multiple DVB network interface(s

    Args:
        net_ifs   : List of DVB network interface names
        ip_addrs  : List of IP addresses for the DVB interface slash subnet mask
        verbose   : Controls verbosity

    """
    if (verbose):
        util._print_header("Interface IP Address")

    is_root = os.geteuid() == 0
    if (not is_root):
        print("Set static IP addresses on dvbnet interfaces.\n")

    # Configure static IP addresses
    if (which("netplan") is None):
        _check_debian_net_interfaces_d(is_root)

    for net_if, ip_addr in zip(net_ifs, ip_addrs):
        _set_ip(net_if, ip_addr, verbose)

    # Bring up interfaces
    if (which("netplan") is not None):
        if (not is_root):
            print("Finally, apply the new netplan configuration:\n")
        util.run_or_print_root_cmd(["netplan", "apply"], logger)
    elif (os.path.exists("/etc/network/interfaces.d/")):
        # Debian approach
        if (not is_root):
            print(textwrap.fill("Finally, restart the networking service and "
                                "bring up the interfaces:") + "\n")
        util.run_or_print_root_cmd(["systemctl", "restart", "networking"],
                                   logger)
    elif (os.path.exists("/etc/sysconfig/network-scripts")):
        # CentOS/Fedora/RHEL approach
        if (not is_root):
            print(textwrap.fill("Finally, bring up the interfaces:") + "\n")

    if (which("netplan") is None):
        for net_if in net_ifs:
            util.run_or_print_root_cmd(["ifup", net_if], logger)


def _check_ips(net_ifs, ip_addrs):
    """Check if IPs of one or multiple DVB network interface(s) are OK

    Args:
        net_ifs   : List of DVB network interface names
        ip_addrs  : List of IP addresses for the DVB interface slash subnet mask
        verbose   : Controls verbosity

    """
    for net_if, ip_addr in zip(net_ifs, ip_addrs):
        has_ip, ip_ok = _check_ip(net_if, ip_addr)
        if (not has_ip):
            raise ValueError("Interface {} does not have an IP address".format(
                net_if))
        elif (has_ip and not ip_ok):
            raise ValueError("Interface {} IP is not {}".format(net_if,
                                                                ip_addr))


def _find_adapter(list_only=False):
    """Find the DVB adapter

    Returns:
        Tuple with (adapter index, frontend index)

    """
    util._print_header("Find DVB Adapter")
    ps     = subprocess.Popen("dmesg", stdout=subprocess.PIPE)
    try:
        output = subprocess.check_output(["grep", "frontend"], stdin=ps.stdout,
                                         stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as grepexc:
        if (grepexc.returncode == 1):
            output = ""
            pass
    ps.wait()

    # Search a range of adapters. There is no command to list all adapters, so
    # we try to list each one individually using `dvbnet -a adapter_no -l`.
    adapters = list()
    for a in range(0,10):
        cmd     = ["dvbnet", "-a", str(a), "-l"]
        logger.debug("> " + " ".join(cmd))

        with open(os.devnull, 'w') as devnull:
            res = subprocess.call(cmd, stdout=devnull, stderr=devnull)
            if (res == 0):
                # Try a few frontends too
                for f in range(0,2):
                    try:
                        output = subprocess.check_output(["dvb-fe-tool", "-a",
                                                          str(a), "-f", str(f)])
                        line   = output.splitlines()[0].decode().split()
                        adapter = {
                            "adapter"  : str(a),
                            "frontend" : line[5].replace(")","").split("frontend")[-1],
                            "vendor"   : line[1],
                            "model"    : " ".join(line[2:4]),
                            "support"  : line[4]
                        }
                        adapters.append(adapter)
                        logger.debug(pformat(adapter))
                    except subprocess.CalledProcessError as e:
                        pass

    # If nothing was obtained using dvbnet, try to inspect dmesg logs
    if (len(adapters) == 0):
        lines       = output.splitlines()
        adapter_set = set() # use set to filter unique values
        adapters    = list()
        for line in lines:
            linesplit = line.decode().split()
            if ("adapter" not in linesplit):
                continue
            i_adapter  = linesplit.index('adapter')
            i_frontend = linesplit.index('frontend')
            device     = linesplit[i_frontend + 2:]
            adapter = {
                "adapter"  : linesplit[i_adapter + 1],
                "frontend" : linesplit[i_frontend + 1],
                "vendor"   : device[0][1:],
                "model"    : " ".join(device[1:-1]),
                "support"  : device[-1][:-4].replace('(', '').replace(')', '')
            }

            # Maybe the device on dmesg does not exist anymore. Check!
            try:
                cmd = ["dvbnet", "-a", adapter['adapter'], "-l"]
                logger.debug("> " + " ".join(cmd))
                res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError as e:
                continue

            # If it exists, add to set of candidate adapters
            adapter_set.add(json.dumps(adapter))

        # Process unique adapter logs
        for adapter in adapter_set:
            adapters.append(json.loads(adapter))
            logger.debug(pformat(json.loads(adapter)))

    dvb_s2_adapters = [a for a in adapters if ("DVB-S/S2" in a["support"])]
    logger.debug(dvb_s2_adapters)

    assert(len(dvb_s2_adapters) > 0), "No DVB-S2 adapters found"

    chosen_adapter = None
    for adapter in dvb_s2_adapters:
        print("Found DVB-S2 adapter: %s %s" %(adapter["vendor"],
                                              adapter["model"]))

        if (not list_only):
            if (len(dvb_s2_adapters) == 1 or
                util._ask_yes_or_no("Choose adapter?")):
                chosen_adapter = adapter
                logger.debug("Chosen adapter:")
                logger.debug(pformat(adapter))
                break

    if (list_only):
        return

    if (chosen_adapter is None):
        raise ValueError("Please choose DVB-S2 adapter")

    return chosen_adapter["adapter"], chosen_adapter["frontend"]


def _dvbnet_single(adapter, ifname, pid, ule, existing_dvbnet_interfaces):
    """Start DVB network interface

    Args:
        adapter                    : DVB adapter index
        ifname                     : DVB network interface name
        pid                        : PID to listen to
        ule                        : Whether to use ULE framing
        existing_dvbnet_interfaces : List of dvbnet interfaces already
                                     configured for the adapter

    """

    assert(pid >= 32 and pid <= 8190), "PID not insider range 32 to 8190"

    if (ule):
        encapsulation = 'ULE'
    else:
        encapsulation = 'MPE'

    # Check if interface already exists
    res = subprocess.call(["ip", "addr", "show", "dev", ifname],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os_interface_exists = (res == 0)
    matching_dvbnet_if  = None

    # When the network interface exists in the OS, we also need to check if the
    # matching dvbnet device is configured according to what we want now
    if (os_interface_exists):
        print("Network interface %s already exists" %(ifname))

        for interface in existing_dvbnet_interfaces:
            if (interface['name'] == ifname):
                matching_dvbnet_if = interface
                break

    # Our indication that interface exists comes from "ip addr show
    # dev". However, it is possible that dvbnet does not have any interface
    # associated to an adapter, so check if we found anything:
    cfg_interface = False
    if (len(existing_dvbnet_interfaces) > 0 and matching_dvbnet_if is not None):
        # Compare to desired configurations
        if (matching_dvbnet_if['pid'] != pid or
            matching_dvbnet_if['encapsulation'] != encapsulation):
            cfg_interface = True

        if (matching_dvbnet_if['pid'] != pid):
            print("Current PID is %d. Set it to %d" %(
                matching_dvbnet_if['pid'], pid))

        if (matching_dvbnet_if['encapsulation'] != encapsulation):
            print("Current encapsulation is %s. Set it to %s" %(
                matching_dvbnet_if['encapsulation'], encapsulation
            ))
    else:
        cfg_interface = True

    # Create interface in case it doesn't exist or needs to be re-created
    if (cfg_interface):
        # If interface exists, but must be re-created, remove the existing one
        # first
        if (os_interface_exists):
            _rm_dvbnet_interface(adapter, ifname, verbose=False)

        adapter_dir = '/dev/dvb/adapter' + adapter

        ule_arg = "-U" if ule else ""

        if (os.geteuid() == 0):
            print("Launch {} using {} encapsulation:".format(
                ifname, "ULE" if ule else "MPE"))

        res = util.run_or_print_root_cmd(["dvbnet", "-a", adapter, "-p",
                                          str(pid), ule_arg], logger)
        if (res is not None):
            print(res.decode())
    else:
        print("Network interface %s already configured correctly" %(ifname))


def _dvbnet(adapter, ifnames, pids, ule=False):
    """Start DVB network interfaces of a DVB adapter

    An adapter can have multiple dvbnet interfaces, one for each PID.

    Args:
        adapter  : DVB adapter index
        ifnames  : list of DVB network interface names
        pids     : List of PIDs to listen to on each interface
        ule      : Whether to use ULE framing

    """
    assert(isinstance(ifnames, list))
    assert(isinstance(pids, list))
    assert(len(ifnames) == len(pids)), \
        "Interface names and PID number must be vectors of the same length"

    # Find the dvbnet interfaces that already exist for the chosen adapter
    existing_dvbnet_iif = _find_dvbnet_interfaces(adapter)

    util._print_header("Network Interface")

    if (os.geteuid() != 0):
        util.fill_print("Launch blocksat-cli as root or run the following \
        commands on your own:")

    for ifname, pid in zip(ifnames, pids):
        _dvbnet_single(adapter, ifname, pid, ule, existing_dvbnet_iif)


def _find_dvbnet_interfaces(adapter):
    """Find dvbnet interface(s) of a DVB adapter

    An adapter can have multiple dvbnet interfaces, one for each PID.

    Args:
        adapter: Corresponding DVB adapter

    Returns:
        interfaces : List of dvbnet interfaces

    """

    util._print_header("Find dvbnet interface(s)")
    cmd     = ["dvbnet", "-a", adapter, "-l"]
    logger.debug("> " + " ".join(cmd))
    res     = subprocess.check_output(cmd)

    interfaces = list()
    for line in res.splitlines():
        if ("Found device" in line.decode()):
            line_split    = line.decode().split()
            interface = {
                'dev'           : line_split[2][:-1],
                'name'          : line_split[4][:-1],
                'pid'           : int(line_split[8][:-1]),
                'encapsulation' : line_split[10]
            }
            logger.debug(pformat(interface))
            interfaces.append(interface)

    if (len(interfaces) == 0):
        print("Could not find any dvbnet interface")

    return interfaces


def _rm_dvbnet_interface(adapter, ifname, verbose=True):
    """Remove DVB net interface

    Args:
        adapter   : Corresponding DVB adapter
        interface : dvbnet interface number
        verbose   : Controls verbosity
    """

    if (verbose):
        util._print_header("Remove dvbnet interface")
    res       = util.run_or_print_root_cmd(["ip", "link", "set",
                                            ifname, "down"], logger)
    if_number = ifname.split("_")[-1]
    res       = util.run_or_print_root_cmd(["dvbnet", "-a", adapter, "-d",
                                            if_number], logger)
    print(res.decode())

    # Remove conf file for network interface (next time the interface could have
    # a different number)
    netplan_file = os.path.join("/etc/netplan/", "blocksat-" + ifname + ".yaml")
    net_file     = os.path.join("/etc/network/interfaces.d/", ifname + ".conf")
    ifcfg_file   = os.path.join("/etc/sysconfig/network-scripts/",
                                "ifcfg-" + ifname)

    if (which("netplan") is not None):
        cfg_file = netplan_file
    elif (os.path.exists(net_file)):
        cfg_file = net_file
    elif (os.path.exists(ifcfg_file)):
        cfg_file = ifcfg_file

    res = util.run_or_print_root_cmd(["rm", cfg_file])
    print("Removed configuration file {}".format(cfg_file))


def zap(adapter, frontend, ch_conf_file, user_info, lnb="UNIVERSAL",
        output=None, timeout=None, monitor=False, scrolling=False):
    """Run zapper

    Args:
        adapter      : DVB adapter index
        frontend     : frontend
        ch_conf_file : Path to channel configurations file
        user_info    : Dictionary with user configurations
        lnb          : LNB type
        output       : Output filename (when recording)
        timeout      : Run the zap for this specified duration
        monitor      : Monitor mode. Monitors DVB traffic stats (throughput and
                       packets per second), but does not deliver data upstream.
        scrolling    : Whether to print zap logs by scrolling rather than
                       printing always on the same line.

    Returns:
        Subprocess object

    """

    util._print_header("Tuning DVB Receiver")
    print("Running dvbv5-zap")

    # LNB name to use when calling dvbv5-zap
    if (lnb is None):
        # Find suitable LNB within v4l-utils preset LNBs
        lnb = _find_v4l_lnb(user_info)['alias']

    cmd = ["dvbv5-zap", "-c", ch_conf_file, "-a", adapter, "-f", frontend,
           "-l", lnb, "-v"]

    if (output is not None):
        cmd = cmd + ["-o", output]

        if (os.path.exists(output)):
            print("File %s already exists" %(output))

            # The recording is such that MPEG TS packets are overwritten one by
            # one. For instance, if previous ts file had 1000 MPEG TS packets,
            # when overwriting, the tool would overwrite each packet
            # individually. So if it was stopped for instance after the first 10
            # MPEG TS packets, only the first 10 would be overwritten, the
            # remaining MPEG TS packets would remain in the ts file.
            #
            # The other option is to remove the ts file completely and start a
            # new one. This way, all previous ts packets are guaranteed to be
            # erased.
            raw_resp = input("Remove and start new (R) or Overwrite (O)? [R/O] ")
            response = raw_resp.lower()

            if (response == "r"):
                os.remove(output)
            elif (response != "o"):
                raise ValueError("Unknown response")

    if (timeout is not None):
        cmd = cmd + ["-t", timeout]

    if (monitor):
        assert(not scrolling), \
            "Monitor mode does not work with scrolling (line-by-line) logs"
        assert(output is None), \
            "Monitor mode does not work if recording (i.e. w/ -r flag)"
        cmd.append("-m")

    cmd.append("blocksat-ch")

    logger.debug("> " + " ".join(cmd))

    if (scrolling):
        ps = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
    else:
        ps = subprocess.Popen(cmd)

    return ps


def subparser(subparsers):
    """Subparser for usb command"""
    p = subparsers.add_parser('usb',
                              description="USB DVB-S2 receiver manager",
                              help='Manage Linux USB DVB-S2 receiver',
                              formatter_class=ArgumentDefaultsHelpFormatter)

    # Common parameters
    p.add_argument('-a', '--adapter',
                   default=None,
                   help='DVB-S2 adapter number')

    p.add_argument('-f', '--frontend',
                   default=None,
                   help='DVB-S2 adapter\'s frontend number')

    p.add_argument('-i', '--ip',
                   default=['192.168.201.2/24', '192.168.201.3/24'],
                   nargs='+',
                   help='IP address set for each DVB-S2 net \
                   interface with subnet mask in CIDR notation')

    p.set_defaults(func=print_help)

    subsubparsers = p.add_subparsers(title='subcommands',
                                         help='Target USB sub-command')

    # Launch
    p1 = subsubparsers.add_parser('launch',
                                  description="Launch a USB DVB-S2 receiver",
                                  help='Launch a Linux USB DVB-S2 receiver')

    p1.add_argument('-c', '--chan-conf',
                    default="channels.conf",
                    help='Channel configurations file within the configuration \
                    directory')

    p1.add_argument('-l', '--lnb',
                    choices=defs.lnb_options,
                    default=None,
                    help="LNB from v4l-utils to be used. "
                    "If None, i.e. not specified, it will be set "
                    "automatically")

    p1.add_argument('-r', '--record-file', default=None,
                    help='Record MPEG-TS traffic into target file')

    p1.add_argument('-t', '--timeout', default=None,
                    help='Stop zapping after timeout - useful to \
                    control recording time')

    p1.add_argument('-m', '--monitor', default=False,
                    action='store_true',
                    help='Launch dvbv5-zap in monitor mode - useful \
                    to debug packet and bit rates')

    p1.add_argument('-s', '--scrolling', default=False,
                    action='store_true',
                    help='Print dvbv5-zap logs line-by-line, i.e. \
                    scrolling, rather than always on the same line')

    p1.add_argument('--logfile', default=False,
                    action='store_true',
                    help='Save dvbv5-zap logs on a file')

    p1.set_defaults(func=launch)

    # Initial configurations
    p2 = subsubparsers.add_parser('config', aliases=['cfg'],
                                  description='Initial configurations',
                                  help='Configure DVB-S2 interface(s) and\
                                  the host')
    p2.add_argument('-U', '--ule', default=False,
                    action='store_true',
                    help='Use ULE encapsulation instead of MPE')

    p2.add_argument('--skip-rp', default=False, action='store_true',
                    help='Skip settting of reverse path filters')

    p2.add_argument('--skip-firewall', default=False,
                    action='store_true',
                    help='Skip configuration of firewall rules')

    p2.add_argument('--pid', default=defs.pids,
                    type=int,
                    nargs='+',
                    help='List of PIDs to be listened to by dvbnet')

    p2.set_defaults(func=usb_config)

    # Find adapter sub-command
    p3 = subsubparsers.add_parser('list', aliases=['ls'],
                                  description="List DVB-S2 adapters",
                                  help='List DVB-S2 adapters',
                                  formatter_class=ArgumentDefaultsHelpFormatter)
    p3.set_defaults(func=list_subcommand)

    # Remove adapter sub-command
    p4 = subsubparsers.add_parser('remove', aliases=['rm'],
                                  description="Remove DVB-S2 adapter",
                                  help='Remove DVB-S2 adapter',
                                  formatter_class=ArgumentDefaultsHelpFormatter)
    p4.add_argument('-a', '--adapter',
                           default=None,
                           help='DVB-S2 adapter number')
    p4.set_defaults(func=rm_subcommand)

    return p


def _common(args):
    # User info
    user_info = config.read_cfg_file(args.cfg_file, args.cfg_dir)

    if (user_info is None):
        return

    # Find adapter
    if (args.adapter is None):
        adapter, frontend = _find_adapter()
    else:
        adapter  = args.adapter
        frontend = args.frontend

    # dvbnet interfaces of interest
    #
    # NOTE: there is one dvbnet interface per PID. Each interface will have a
    # different IP address.
    net_ifs  = list()
    for i_device in range(0, len(args.ip)):
        # Define interface name that is going to be generated by dvbnet
        net_if = "dvb" + adapter + "_" + str(i_device)
        net_ifs.append(net_if)

    return user_info, adapter, frontend, net_ifs


def usb_config(args):
    """Config the DVB interface(s) and the host"""
    assert(len(args.pid) == len(args.ip)), \
        "Number of PIDs (%u) defined by argument --pid " %(len(args.pid)) + \
        "does not match the number of IPs (%u) defined by " %(len(args.ip)) + \
        "argument --ip. Please define one IP address for each PID."

    if (os.geteuid() != 0):
        print("WARNING:\n")
        util.fill_print("\"blocksat-cli usb init\" requires root access. \
        Please run as root or follow the instructions below.")

    common_params = _common(args)
    if (common_params is None):
        return
    user_info, adapter, frontend, net_ifs = common_params

    # Create the dvbnet interface(s)
    _dvbnet(adapter, net_ifs, args.pid, ule=args.ule)

    # Set RP filters
    if (not args.skip_rp):
        rp.set_rp_filters(net_ifs)

    # Set firewall rules
    if (not args.skip_firewall):
        firewall.configure(net_ifs, defs.src_ports)

    # Set IP
    _set_ips(net_ifs, args.ip)

    util._print_header("Next Step")
    print("Run:\n\nblocksat-cli usb launch\n")

    return


def launch(args):
    """Launch the DVB interface from scratch

    Handles the launch subcommand

    """

    common_params = _common(args)
    if (common_params is None):
        return
    user_info, adapter, frontend, net_ifs = common_params

    # Channel configuration file
    chan_conf = os.path.join(args.cfg_dir, os.path.basename(args.chan_conf))

    # Zap
    zap_ps = zap(adapter, frontend, chan_conf, user_info, lnb=args.lnb,
                 output=args.record_file, timeout=args.timeout,
                 monitor=args.monitor, scrolling=(args.scrolling
                                                  or args.logfile))

    # Prepare logs
    logfile = None if (not args.logfile) else _setup_logfile(args.cfg_dir)

    # Handler for SIGINT
    def signal_handler(sig, frame):
        print('Stopping...')
        zap_ps.terminate()
        sys.exit(zap_ps.poll())

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Timer to periodically check the interface IP
    def ip_checking_timer():
        try:
            _check_ips(net_ifs, args.ip)
        except ValueError as e:
            print('Stopping...')
            zap_ps.terminate()
            raise e
        timer        = threading.Timer(10, ip_checking_timer)
        timer.daemon = True
        timer.start()

    ip_checking_timer()

    # Listen to dvbv5-zap indefinitely
    if (args.scrolling or args.logfile):
        # Loop indefinitely over zap
        prev_line = None
        while (zap_ps.poll() is None):
            line = zap_ps.stderr.readline()
            if (line and line != "\n"):
                pretty_line = '\r{}: '.format(
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())) + line
                if (("Signal" in line) and ("Layer" not in line)):
                    prev_line = pretty_line.replace("\n", " ")
                else:
                    concat_line = pretty_line if prev_line is None else \
                                  (prev_line + line)
                    final_line  = " ".join(concat_line.split()) + "\n"
                    prev_line   = None
                    print(final_line, end='')
                    if (logfile is not None):
                        with open(logfile, 'a') as fd:
                            fd.write(final_line)
            else:
                time.sleep(1)
    else:
        zap_ps.wait()
    sys.exit(zap_ps.poll())


def list_subcommand(args):
    """Call function that finds the DVB adapter

    Handles the find-adapter subcommand

    """
    _find_adapter(list_only=True)


def rm_subcommand(args):
    """Remove DVB interface

    """

    if (os.geteuid() != 0):
        logger.error("Root access is required to remove dvbnet interfaces.")
        print("Please, run as root.")
        return

    # Find adapter
    if (args.adapter is None):
        adapter, frontend = _find_adapter()
    else:
        adapter = args.adapter

    interfaces     = _find_dvbnet_interfaces(adapter)
    chosen_devices = list()

    if (len(interfaces) > 1):
        print("Choose net device to remove:")
        for i_dev, interface in enumerate(interfaces):
            print("[%2u] %s" %(i_dev, interface['name']))
        print("[ *] all")

        try:
            choice = input("Choose number: ")
            if (choice == "*"):
                i_chosen_devices = range(0, len(interfaces))
            else:
                i_chosen_devices = [int(choice)]
        except ValueError:
            raise ValueError("Please choose a number or \"*\" for all devices")

        for i_chosen_dev in i_chosen_devices:
            if (i_chosen_dev > len(interfaces)):
                raise ValueError("Invalid number")

            chosen_devices.append(interfaces[i_chosen_dev]['name'])

    elif (len(interfaces) == 0):
        print("No DVB network interfaces to remove")
        return
    else:
        # There is a single interface
        chosen_devices.append(interfaces[0]['name'])

    for chosen_dev in chosen_devices:
        if (not util._ask_yes_or_no("Remove interface %s?" %(chosen_dev))):
            print("Aborting...")
            return

        _rm_dvbnet_interface(adapter, chosen_dev)


def print_help(args):
    """Re-create argparse's help menu for the usb command"""
    parser     = ArgumentParser()
    subparsers = parser.add_subparsers(title='', help='')
    parser     = subparser(subparsers)
    print(parser.format_help())

