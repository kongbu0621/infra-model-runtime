"""Internal bounded record validation, independent of consumer projections.

Checksums detect byte changes; these checks reject inconsistent writer output.
They do not authenticate history against a hostile/coherent database rewrite.
"""
from dataclasses import asdict
import re
from .contracts import WireObservation, final_usage
from .contracts import (Observation, Profile, Receipt, Request, RuntimeFailure,
                        encode, fingerprint, identifier, integer, require, restore)


def shape(value, fields):
    require(type(value) is dict and set(value) == set(fields), 'INTEGRITY_FAILURE')


def digest(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None,
            'INTEGRITY_FAILURE')


def exact_receipt(value, operation, identity, revision, signature):
    # JSON equality distinguishes int/bool/float and checks exact keys/version.
    expected = asdict(Receipt(operation, identity, revision, signature))
    require(encode(value) == encode(expected), 'INTEGRITY_FAILURE')


def validate_call(value, namespace, request_id):
    try:
        require(type(value) is dict and type(value.get('issued')) is bool, 'INTEGRITY_FAILURE')
        fields = {'request', 'profile', 'configuration_revision', 'receipt', 'issued',
                  'cancelled', 'terminal', 'outcome', 'output_text', 'output_units',
                  'usage_final', 'output_reserved', 'observation_revision',
                  'observations', 'cancellations', 'quarantined'}
        shape(value, fields | ({'permit_digest'} if value['issued'] else set()))
        request = restore(Request, value['request'])
        profile = restore(Profile, value['profile'])
        require((request.namespace, request.request_id) == (namespace, request_id), 'INTEGRITY_FAILURE')
        integer(value['configuration_revision'], 1)
        require(profile.enabled and profile.name in request.allowed_profiles and
                set(request.required_capabilities) <= set(profile.capabilities), 'INTEGRITY_FAILURE')
        exact_receipt(value['receipt'], 'register', (namespace, request_id),
                      value['configuration_revision'], fingerprint(value['request']))
        if value['issued']:
            digest(value['permit_digest'])
        cancellations = value['cancellations']
        require(type(cancellations) is dict and len(cancellations) <= 8, 'INTEGRITY_FAILURE')
        for control_id, receipt in cancellations.items():
            identifier(control_id)
            identity = (namespace, request_id, control_id)
            exact_receipt(receipt, 'cancel', identity, 1, fingerprint(list(identity)))
        observations = value['observations']
        require(type(observations) is dict and len(observations) <= 16, 'INTEGRITY_FAILURE')
        require(value['issued'] or not observations, 'INTEGRITY_FAILURE')
        for key in observations:
            require(type(key) is str and key in {str(i) for i in range(1, 17)}, 'INTEGRITY_FAILURE')
        expected = dict(cancelled=bool(cancellations), terminal=False, outcome='unknown',
                        output_text=None, output_units=None, usage_final=False,
                        output_reserved=request.output_limit if value['issued'] else 0,
                        observation_revision=0, quarantined=False)
        for key in sorted(observations, key=int):
            entry = observations[key]
            shape(entry, ('observation', 'receipt'))
            observation = restore(Observation, entry['observation'])
            require((request.version, observation.version) in (
                ('r1.request.v1', 'r1.observation.v1'), ('r2.request.v1', 'r2.observation.v1')),
                'INTEGRITY_FAILURE')
            require(observation.revision == int(key), 'INTEGRITY_FAILURE')
            exact_receipt(entry['receipt'], 'observe', (namespace, request_id),
                          observation.revision, fingerprint(entry['observation']))
            if expected['terminal']:
                require(observation.terminal and observation.outcome == expected['outcome'] and
                        observation.output_text == expected['output_text'], 'INTEGRITY_FAILURE')
            known = expected['output_units']
            if observation.output_units is not None:
                require(known is None or observation.output_units >= known, 'INTEGRITY_FAILURE')
                known = observation.output_units
            final = expected['usage_final'] or final_usage(observation)
            expected.update(terminal=observation.terminal, outcome=observation.outcome,
                output_text=observation.output_text, output_units=known, usage_final=final,
                output_reserved=0 if final else max(0, request.output_limit - (known or 0)),
                observation_revision=observation.revision,
                quarantined=expected['quarantined'] or (known or 0) > request.output_limit or getattr(observation, "quarantine", False))
        require(encode({key: value[key] for key in expected}) == encode(expected), 'INTEGRITY_FAILURE')
        return value
    except (KeyError, TypeError, ValueError, RuntimeFailure):
        raise RuntimeFailure('INTEGRITY_FAILURE') from None


def validate_control(value, operation, control_id):
    try:
        require(operation in ('configure', 'application'), 'INTEGRITY_FAILURE')
        identifier(control_id)
        shape(value, ('intent', 'receipt'))
        digest(value['intent'])
        revision = value['receipt']['revision']
        integer(revision, 1)
        require(revision >= 2 if operation == 'configure' else revision == 1, 'INTEGRITY_FAILURE')
        exact_receipt(value['receipt'], operation, (control_id,), revision, value['intent'])
        return revision
    except (KeyError, TypeError, ValueError, RuntimeFailure):
        raise RuntimeFailure('INTEGRITY_FAILURE') from None
