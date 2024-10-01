ARG VERSION=master-ubuntu_jammy
FROM openstackhelm/cinder:${VERSION} as build
RUN apt update && apt install -y git
RUN /var/lib/openstack/bin/pip install --upgrade --force-reinstall pip
WORKDIR /opt/cinder-rxt
COPY . /opt/cinder-rxt
RUN ls -al /opt/cinder-rxt/
RUN /var/lib/openstack/bin/pip install --no-cache-dir -e git+file:///opt/cinder-rxt#egg=cinder-rxt
RUN find /var/lib/openstack -regex '^.*\(__pycache__\|\.py[co]\)$' -delete

FROM openstackhelm/cinder:${VERSION}
COPY --from=build /var/lib/openstack/. /var/lib/openstack/
