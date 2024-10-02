# Cinder RXT Driver

The Cinder RXT Driver is a plugin for OpenStack Cinder that enables integration with Rackspace's RXTLVM
storage solution. This driver allows for seamless volume management and provisioning within an OpenStack
environment.

## Installation

To install the Cinder RXT Driver, follow these steps:

``` shell
pip install git+https://github.com/rackerlabs/cinder-rxt.git
```

## Configuration

To configure the Cinder RXT Driver, add the following to your `cinder.conf` file.

``` conf
[DEFAULT]
enabled_backends = RXTlvm

[RXTlvm]
volume_driver = cinder_rxt.rackspace.RXTLVM
...
```
