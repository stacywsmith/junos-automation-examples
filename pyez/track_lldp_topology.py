#!/usr/bin/env python
"""Use apply-macro configurations to track the topology reported by LLDP.

This includes the following steps:
1) Gather current LLDP neighbor information.
2) Gather previous LLDP information from apply-macro configurations.
3) Compare current LLDP neighbor info to previous LLDP info from the
   apply-macro and print LLDP Up / Change / Down messages.
4) Store the current LLDP neighbor info in the apply-macro configurations.

The apply-macro configurations are in the format:
apply-macro LLDP {
    system <remote system>;
    port <remote port>;
    state <Up/Down>;
}

The 'Down' state value indicates an LLDP neighbor which was previously present,
but is now not present.
"""

import sys
import getpass

from lxml.etree import XML
import jxmlease

from jnpr.junos import Device
from jnpr.junos.utils.config import Config
import jnpr.junos.exception

try:
    user_input = raw_input
except NameError:
    user_input = input

TEMPLATE_PATH = 'interface_apply_macro_template.xml'

# Create a jxmlease parser with desired defaults.
parser = jxmlease.EtreeParser()


class DoneWithDevice(Exception):
    pass


def main():
    """The main loop.

    Prompt for a username and password.
    Loop over each device specified on the command line.
    Perform the following steps on each device:
    1) Get LLDP information from the current device state.
    2) Get previous snapshot of LLDP state from the apply-macro configurations.
    3) Compare the LLDP information against the previous snapshot of LLDP
       information. Print changes.
    4) Build a configuration snippet with new apply-macro configurations.
    5) Commit the configuration changes.

    Return an integer suitable for passing to sys.exit().
    """

    if len(sys.argv) == 1:
        print("\nUsage: %s device1 [device2 [...]]\n\n" % sys.argv[0])
        return 1

    rc = 0

    # Get username and password as user input.
    user = user_input('Device Username: ')
    password = getpass.getpass('Device Password: ')

    for hostname in sys.argv[1:]:
        try:
            print("Connecting to %s..." % hostname)
            dev = Device(host=hostname,
                         user=user,
                         password=password,
                         normalize=True)
            dev.open()

            print("Getting current LLDP snapshot from %s..." % hostname)
            current_lldp_info = get_lldp_neighbors(device=dev)
            if current_lldp_info is None:
                print("    Error retrieving LLDP info on " + hostname +
                      ". Make sure LLDP is enabled.")
                rc = 1
                raise DoneWithDevice

            print("Getting previous LLDP snapshot from %s..." % hostname)
            previous_lldp_info = get_lldp_apply_macro(device=dev)
            if previous_lldp_info is None:
                print("    Error retrieving LLDP apply-macro configurations on"
                      " %s." % hostname)
                rc = 1
                raise DoneWithDevice

            changes = check_lldp_changes(current_lldp_info, previous_lldp_info)
            if not changes:
                print("    No LLDP changes to configure on %s." % hostname)
                raise DoneWithDevice

            if load_merge_template_config(device=dev,
                                          template_path=TEMPLATE_PATH,
                                          template_vars={'lldp': changes}):
                print("    Successfully committed configuration changes on "
                      "%s." % hostname)
            else:
                print("    Error committing description changes on %s." %
                      hostname)
                rc = 1
                raise DoneWithDevice
        except jnpr.junos.exception.ConnectError as err:
            print("    Error connecting: " + repr(err))
            rc = 1
        except DoneWithDevice:
            pass
        finally:
            print("    Closing connection to %s." % hostname)
            try:
                dev.close()
            except:
                pass
    return rc


def get_lldp_neighbors(device):
    """Get current LLDP neighbor information.

    Return a two-level dictionary with the LLDP neighbor information..
    The first-level key is the local port (aka interface) name.
    The second-level keys are 'system' for the remote system name
    and 'port' for the remote port ID. On error, return None.

    For example:
    {'ge-0/0/1': {'system': 'r1', 'port', 'ge-0/0/10'}}
    """

    lldp_info = {}
    try:
        resp = device.rpc.get_lldp_neighbors_information()
    except (jnpr.junos.exception.RpcError,
            jnpr.junos.exception.ConnectError)as err:
        print("    " + repr(err))
        return None

    for nbr in resp.findall('lldp-neighbor-information'):
        local_port = nbr.findtext('lldp-local-port-id')
        remote_system = nbr.findtext('lldp-remote-system-name')
        remote_port = nbr.findtext('lldp-remote-port-id')
        if local_port and (remote_system or remote_port):
            lldp_info[local_port] = {'system': remote_system,
                                     'port': remote_port}

    return lldp_info


def get_lldp_apply_macro(device):
    """Get LLDP apply-macro configuration for each interface.

    Return a two-level dictionary. The first-level key is the
    local port (aka interface) name. The second-level keys are
    'system' for the remote system name, 'port' for the remote port, and 'down'
    which is a boolean indicating if LLDP was previously down. On error,
    return None.

    For example:
    {'ge-0/0/1': {'system': 'r1', 'port': 'ge-0/0/10', 'down': True}}
    """
    lldp_info = {}
    try:
        conf = parser(device.rpc.get_config(
            filter_xml=XML('<configuration><interfaces/></configuration>'),
            options={'database': 'committed'}))
    except (jnpr.junos.exception.RpcError,
            jnpr.junos.exception.ConnectError)as err:
        print("    " + repr(err))
        return None
    try:
        pi = conf['configuration']['interfaces']['interface'].jdict()
    except KeyError:
        return lldp_info

    for (local_port, port_info) in pi.items():
        try:
            macros = port_info['apply-macro'].jdict()
            for (name, macro_info) in macros.items():
                if name == 'LLDP':
                    lldp_info[local_port] = {}
                    data = macro_info['data'].jdict()
                    down = False
                    for (name, info) in data.items():
                        if name == 'system':
                            lldp_info[local_port]['system'] = info['value']
                        if name == 'port':
                            lldp_info[local_port]['port'] = info['value']
                        if name == 'state':
                            if info['value'] is 'Down':
                                down = True
                    lldp_info[local_port]['down'] = down
        except (KeyError, TypeError):
            pass
    return lldp_info


def check_lldp_changes(current_lldp_info, previous_lldp_info):
    """Compare current LLDP info with previous snapshot from configuration.

    Given the dictionaries produced by get_lldp_neighbors() and
    get_lldp_apply_macro(), print LLDP up, change, and down messages.

    Return a dictionary containing information for the new apply-macros
    to configure.
    """

    changes = {}

    # Iterate through the current LLDP neighbor state. Compare this
    # to the previous state as retreived from the apply-macros configuration.
    for local_port in current_lldp_info:
        current_system = current_lldp_info[local_port]['system']
        current_port = current_lldp_info[local_port]['port']
        has_macro = local_port in previous_lldp_info
        if has_macro:
            previous_system = previous_lldp_info[local_port]['system']
            previous_port = previous_lldp_info[local_port]['port']
            down = previous_lldp_info[local_port]['down']
            if not previous_system or not previous_port:
                has_macro = False
        if not has_macro:
            print("    %s LLDP Up. Now: %s %s" %
                  (local_port, current_system, current_port))
        elif down:
            print("    %s LLDP Up. Was: %s %s Now: %s %s" %
                  (local_port, previous_system, previous_port,
                   current_system, current_port))
        elif (current_system != previous_system or
              current_port != previous_port):
            print("    %s LLDP Change. Was: %s %s Now: %s %s" %
                  (local_port, previous_system, previous_port,
                   current_system, current_port))
        else:
            # No change. LLDP was not down. Same system and port.
            continue
        changes[local_port] = {}
        changes[local_port]['system'] = current_system
        changes[local_port]['port'] = current_port
        changes[local_port]['state'] = 'Up'

    # Iterate through the previous state as retrieved from the apply-macros
    # configuration. Look for any neighbors that are present in the
    # previous state, but are not present in the current LLDP neighbor
    # state.
    for local_port in previous_lldp_info:
        previous_system = previous_lldp_info[local_port]['system']
        previous_port = previous_lldp_info[local_port]['port']
        down = previous_lldp_info[local_port]['down']
        if (previous_system and previous_port and not down and
           local_port not in current_lldp_info):
            print("    %s LLDP Down. Was: %s %s" %
                  (local_port, previous_system, previous_port))
            changes[local_port] = {}
            changes[local_port]['system'] = previous_system
            changes[local_port]['port'] = previous_port
            changes[local_port]['state'] = 'Down'

    return changes


def load_merge_template_config(device,
                               template_path,
                               template_vars):
    """Load templated config with "configure private" and "load merge".

    Given a template_path and template_vars, do:
        configure private,
        load merge of the templated config,
        commit,
        and check the results.

    Return True if the config was committed successfully, False otherwise.
    """

    class LoadNotOKError(Exception):
        pass

    device.bind(cu=Config)

    rc = False

    try:
        try:
            resp = device.rpc.open_configuration(private=True)
        except jnpr.junos.exception.RpcError as err:
            if not (err.rpc_error['severity'] == 'warning' and
                    'uncommitted changes will be discarded on exit' in
                    err.rpc_error['message']):
                raise

        resp = device.cu.load(template_path=template_path,
                              template_vars=template_vars,
                              merge=True)
        if resp.find("ok") is None:
            raise LoadNotOKError
        device.cu.commit(comment="made by %s" % sys.argv[0])
    except (jnpr.junos.exception.RpcError,
            jnpr.junos.exception.ConnectError,
            LoadNotOKError) as err:
        print("    " + repr(err))
    except:
        print("    Unknown error occured loading or committing configuration.")
    else:
        rc = True
    try:
        device.rpc.close_configuration()
    except jnpr.junos.exception.RpcError as err:
        print("    " + repr(err))
        rc = False
    return rc


if __name__ == "__main__":
    sys.exit(main())
