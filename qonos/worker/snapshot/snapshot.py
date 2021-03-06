# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright 2013 Rackspace
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import calendar
import datetime
from operator import attrgetter
import time

from novaclient import exceptions
from oslo.config import cfg

from qonos.common import timeutils
from qonos.openstack.common.gettextutils import _
from qonos.openstack.common import importutils
import qonos.openstack.common.log as logging
import qonos.qonosclient.exception as qonos_ex
from qonos.worker import worker


LOG = logging.getLogger(__name__)

snapshot_worker_opts = [
    cfg.StrOpt('nova_client_factory_class',
               default='qonos.worker.snapshot.simple_nova_client_factory.'
                       'NovaClientFactory'),
    cfg.IntOpt('image_poll_interval_sec', default=0,
               help=_('How often to poll Nova for the image status')),
    cfg.IntOpt('job_update_interval_sec', default=300,
               help=_('How often to update the job status, in seconds')),
    cfg.IntOpt('job_timeout_update_interval_min', default=60,
               help=_('How often to update the job timeout, in minutes')),
    cfg.IntOpt('job_timeout_update_increment_min', default=60,
               help=_('How much to increment the timeout, in minutes')),
    cfg.IntOpt('job_timeout_max_updates', default=3,
               help=_('How many times to update the timeout before '
                      'considering the job to be failed')),
]

CONF = cfg.CONF
CONF.register_opts(snapshot_worker_opts, group='snapshot_worker')


class SnapshotProcessor(worker.JobProcessor):
    def __init__(self):
        super(SnapshotProcessor, self).__init__()
        self.status_map = {
            "QUEUED": "PROCESSING",
            "SAVING": "PROCESSING",
            "ACTIVE": "DONE",
            "KILLED": "ERROR",
            "DELETED": "ERROR",
            "PENDING_DELETE": "ERROR",
            "ERROR": "ERROR"
        }

    def init_processor(self, worker, nova_client_factory=None):
        super(SnapshotProcessor, self).init_processor(worker)
        self.current_job = None
        self.timeout_count = 0
        self.timeout_max_updates = CONF.snapshot_worker.job_timeout_max_updates
        self.next_timeout = None
        self.update_interval = datetime.timedelta(
            seconds=CONF.snapshot_worker.job_update_interval_sec)
        self.timeout_increment = datetime.timedelta(
            minutes=CONF.snapshot_worker.job_timeout_update_increment_min)
        self.image_poll_interval = CONF.snapshot_worker.image_poll_interval_sec

        if not nova_client_factory:
            nova_client_factory = importutils.import_object(
                CONF.snapshot_worker.nova_client_factory_class)
        self.nova_client_factory = nova_client_factory

    def process_job(self, job):
        LOG.info(_("Worker %(worker_id)s Processing job: %(job)s") %
                    {'worker_id': self.worker.worker_id,
                     'job': job['id']})
        LOG.debug(_("Worker %(worker_id)s Processing job: %(job)s") %
                    {'worker_id': self.worker.worker_id,
                     'job': str(job)})
        payload = {'job': job}
        if job['status'] == 'QUEUED':
            self.send_notification_start(payload)
        else:
            self.send_notification_retry(payload)
        job_id = job['id']
        if not self._check_schedule_exists(job):
            msg = ('Schedule %(schedule_id)s deleted for job %(job_id)s' %
                   {'schedule_id': job['schedule_id'], 'job_id': job_id})
            self._job_cancelled(job_id, msg)

            LOG.info(_('Worker %(worker_id)s Job cancelled: %(msg)s') %
                      {'worker_id': self.worker.worker_id,
                       'msg': msg})
            return

        self.current_job = job

        now = self._get_utcnow()
        self.next_timeout = now + self.timeout_increment
        self.update_job(job_id, 'PROCESSING', timeout=self.next_timeout)
        self.next_update = self._get_utcnow() + self.update_interval

        instance_id = self._get_instance_id(job)
        if not instance_id:
            msg = ('Job %s does not specify an instance_id in its metadata.'
                   % job_id)
            self._job_cancelled(job_id, msg)
            return

        if ('image_id' in job['metadata'] and
            job['status'] in ['PROCESSING', 'TIMED_OUT']):
            image_id = job['metadata']['image_id']
            LOG.info(_("Worker %(worker_id)s Resuming image: %(image_id)s") %
                      {'worker_id': self.worker.worker_id,
                       'image_id': image_id})
        else:
            metadata = {
                "org.openstack__1__created-by": "scheduled_images_service"
                }

            try:
                server_name = self._get_nova_client().servers.\
                    get(instance_id).name
                image_id = self._get_nova_client().servers.create_image(
                    instance_id,
                    self.generate_image_name(server_name),
                    metadata)
            except exceptions.NotFound:
                msg = ('Instance %(instance_id)s specified by job %(job_id)s '
                       'was not found.' %
                     {'instance_id': instance_id, 'job_id': job_id})
                self._job_cancelled(job_id, msg)
                return

            LOG.info(_("Worker %(worker_id)s Started create image: "
                       " %(image_id)s") % {'worker_id': self.worker.worker_id,
                                          'image_id': image_id})

            self._add_job_metadata(image_id=image_id)

        image_status = None
        active = False
        retry = True

        status = None
        while retry and not active:
            status = self._get_image_status(image_id)
            if self._is_error_status(status):
                break

            active = self._is_active_status(status)
            if active:
                self._job_succeeded(job_id)
            else:
                retry = self._try_update(job_id, status['job_status'])

            if not active:
                time.sleep(self.image_poll_interval)

        if (not active) and (not retry):
            self._job_timed_out(job_id)

        if active:
            self._process_retention(instance_id)
            self.send_notification_end(payload)

        LOG.debug("Snapshot complete")

    def cleanup_processor(self):
        """
        Override to perform processor-specific setup.

        Called AFTER the worker is unregistered from QonoS.
        """
        pass

    def generate_image_name(self, server_name):
        """
        Creates a string based on the specified server name and current time.

        The string is of the format:
        "Daily-<truncated-server-name>-<unix-timestamp>"
        """
        prefix = 'Daily-'
        max_name_length = 255
        now = str(calendar.timegm(self._get_utcnow().utctimetuple()))

        #NOTE(ameade): Truncate the server name so the image name is within
        # 255 characters total
        server_name_len = max_name_length - len(now) - len(prefix) - len('-')
        server_name = server_name[:server_name_len]

        return (prefix + server_name + '-' + str(now))

    def _add_job_metadata(self, **to_add):
        metadata = self.current_job['metadata']
        for key in to_add:
            metadata[key] = to_add[key]

        self.current_job['metadata'] = self.update_job_metadata(
            self.current_job['id'], metadata)

    def _process_retention(self, instance_id):
        LOG.debug(_("Processing retention."))
        retention = self._get_retention(instance_id)

        if retention > 0:
            scheduled_images = self._find_scheduled_images_for_server(
                instance_id)

            if len(scheduled_images) > retention:
                to_delete = scheduled_images[retention:]
                LOG.info(_('Worker %(worker_id)s '
                           'Removing %(remove)d images for a retention '
                           'of %(retention)d')
                          % {'worker_id': self.worker.worker_id,
                             'remove': len(to_delete),
                             'retention': retention})
                for image in to_delete:
                    image_id = image.id
                    self._get_nova_client().images.delete(image_id)
                    LOG.info(_('Worker %(worker_id)s Removed image '
                               '%(image_id)s')
                              % {'worker_id': self.worker.worker_id,
                                 'image_id': image_id})

    def _get_retention(self, instance_id):
        ret_str = None
        retention = 0
        try:
            result = self._get_nova_client().\
                rax_scheduled_images_python_novaclient_ext.get(instance_id)
            ret_str = result.retention
            retention = int(ret_str or 0)
        except exceptions.NotFound, e:
            msg = _('Could not retrieve retention for server %s: either the'
                    ' server was deleted or scheduled images for'
                    ' the server was disabled.') % instance_id
            LOG.warn(msg)
        except Exception, e:
            msg = _('Error getting retention for server %s: ')
            LOG.exception(msg % instance_id)

        return retention

    def _find_scheduled_images_for_server(self, instance_id):
        images = self._get_nova_client().images.list(detailed=True)
        scheduled_images = []
        for image in images:
            metadata = image.metadata
            if (metadata.get("org.openstack__1__created_by")
                == "scheduled_images_service" and
                metadata.get("instance_uuid") == instance_id):
                scheduled_images.append(image)

        scheduled_images = sorted(scheduled_images,
                                  key=attrgetter('created'),
                                  reverse=True)

        return scheduled_images

    def _is_error_status(self, status):
        job_status = status['job_status']
        if job_status == 'ERROR':
            instance_id = self._get_instance_id(self.current_job)
            msg = (('Error occurred while taking snapshot: '
                    'Instance: %(instance_id)s, image: %(image_id)s, '
                     'status: %(image_status)s') %
                   {'instance_id': instance_id,
                    'image_id': status['image_id'],
                    'image_status': status['image_status']})
            LOG.warn(msg)
            self._job_failed(self.current_job['id'], msg)
            return True
        return False

    def _is_active_status(self, status):
        job_id = self.current_job['id']
        job_status = status['job_status']
        image_status = status['image_status']
        active = image_status == 'ACTIVE'
        return active

    def _get_image_status(self, image_id):
        image = self._get_nova_client().images.get(image_id)
        if image:
            image_status = image.status
        else:
            image_status = 'KILLED'

        return {'image_id': image_id,
                'image_status': image_status,
                'job_status': self.status_map[image_status]}

    def _get_nova_client(self):
        nova_client = self.nova_client_factory.get_nova_client(
            self.current_job)
        return nova_client

    def _job_succeeded(self, job_id):
        self.update_job(job_id, 'DONE')

    def _job_timed_out(self, job_id):
        self.update_job(job_id, 'TIMED_OUT')

    def _job_failed(self, job_id, error_message):
        self.update_job(job_id, 'ERROR', error_message=error_message)

    def _job_cancelled(self, job_id, message):
        self.update_job(job_id, 'CANCELLED', error_message=message)

    def _try_update(self, job_id, status):
        now = self._get_utcnow()
        # Time for a timeout update?
        if now >= self.next_timeout:
            # Out of timeouts?
            if self.timeout_count >= self.timeout_max_updates:
                return False
            self.next_timeout = self.next_timeout + self.timeout_increment
            self.timeout_count += 1
            self.update_job(job_id, status, self.next_timeout)
            return True

        # Time for a status-only update?
        if now >= self.next_update:
            self.next_update = now + self.update_interval
            self.update_job(job_id, status)

        return True

    def _get_instance_id(self, job):
        metadata = job['metadata']
        return metadata.get('instance_id')

    def _get_username(self, job):
        metadata = job['metadata']
        return metadata.get('user_name')

    def _check_schedule_exists(self, job):
        qonosclient = self.get_qonos_client()
        try:
            qonosclient.get_schedule(job['schedule_id'])
            return True
        except qonos_ex.NotFound, ex:
            return False

    # Seam for testing
    def _get_utcnow(self):
        return timeutils.utcnow()
