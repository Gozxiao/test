"""Image IDs and OCI digests have different namespaces, on both Docker stores."""
import json
import unittest
import executor
import controller


class ImageIdentity(unittest.TestCase):
    def test_classic_null_descriptor_uses_config_lookup(self):
        config = 'sha256:' + 'a' * 64
        seen = []
        def call(*args):
            seen.append(args)
            return json.dumps({'Image':config, 'ImageManifestDescriptor':None} if '--type' in args
                else {'Id':config, 'Os':'linux', 'Architecture':'amd64'})
        self.assertEqual(executor.running_image(call, 'container'), (config, config, None))
        self.assertEqual(seen[-1][-1], config)

    def test_containerd_resolves_manifest_without_looking_up_config(self):
        config, manifest, platform_id = ['sha256:' + c*64 for c in 'abc']
        descriptor = {'digest':manifest, 'mediaType':'application/vnd.oci.image.manifest.v1+json'}
        def call(*args):
            if '--type' in args:return json.dumps({'Image':config, 'ImageManifestDescriptor':descriptor})
            self.assertEqual(args[-1], manifest)
            return json.dumps({'Id':platform_id,'Os':'linux','Architecture':'amd64'})
        self.assertEqual(executor.running_image(call, 'container'), (platform_id, config, descriptor))

    def test_invalid_ids_and_platform_fail_closed(self):
        for obj in ({'Id':None,'Os':'linux','Architecture':'amd64'},
                    {'Id':'sha256:'+'a'*64,'Os':'linux','Architecture':'arm64'}):
            with self.assertRaises(RuntimeError):executor.platform_image(lambda *a:json.dumps(obj), 'ref')

    def test_invalid_descriptor_is_not_a_classic_fallback(self):
        for desc in ({}, {'digest':'not-a-digest','mediaType':'application/vnd.oci.image.manifest.v1+json'}):
            with self.assertRaises(RuntimeError):
                executor.running_image(lambda *a:json.dumps({'Image':'sha256:'+'a'*64,'ImageManifestDescriptor':desc}), 'container')

    def test_new_receipt_id_is_authoritative_over_legacy_manifest(self):
        result={'status':'ready','target_sha':'a'*40,'checkout_sha':'a'*40,
            'backend_health':True,'public_frontend':True,'frontend_sha256':'f'*64,'assets_checked':1,
            'built':{'backend':'b','web':'w'},'running':{'backend':{'image_id':'b'},'web':{'image_id':'w'}}}
        self.assertFalse(controller.replacement_unconfirmed(result))
        result['running']['web']={'image_id':None,'manifest':'w'}
        self.assertTrue(controller.replacement_unconfirmed(result))


if __name__ == '__main__':unittest.main()
