"""Independent synthetic speech package with its own voice parameters."""
import math
import wave


class Adapter:
    def describe(self,config):
        return dict(protocol='media-adapter/1',engine='fixture-speech',kinds=['speech'],resource='gpu:0',
            summary='Synthetic PCM audition for extension verification.',operations=['synthesize','audition'],
            voice_reference_required=True,parameters_schema=dict(type='object',properties={},additionalProperties=False),
            voice_parameters_schema=dict(type='object',properties=dict(tone=dict(type='number',minimum=100,maximum=800)),additionalProperties=False),
            output=dict(kind='audio',mime='audio/wav',extension='.wav'),cancel_grace_seconds=0)

    def normalize_voice(self,parameters,config):
        from life.agent_session import SessionFailure
        if not isinstance(parameters,dict) or set(parameters)-{'tone'}:
            raise SessionFailure('invalid_input','Synthetic voice supports only tone')
        tone=parameters.get('tone',330)
        if isinstance(tone,bool) or not isinstance(tone,(int,float)) or not math.isfinite(tone) or not 100<=tone<=800:
            raise SessionFailure('invalid_input','Synthetic tone must be 100..800')
        return dict(tone=tone)

    def prepare(self,p,assets,config,voice=None):
        if voice is None:
            raise ValueError('audition_voice_required')
        parameters=self.normalize_voice(voice['parameters'],config)
        return dict(kind='speech',text=p.get('text','Synthetic audition'),seed=p.get('seed'),
            voice_id=voice['id'],voice_revision=voice['revision'],reference_asset_id=voice['reference_asset_id'],
            reference_sha256=voice['reference_digest'],parameters=parameters)

    def execute(self,spec,context):
        import struct
        output=context.folder/'output.wav'
        with wave.open(str(output),'wb') as target:
            target.setnchannels(1);target.setsampwidth(2);target.setframerate(16000)
            target.writeframes(b''.join(struct.pack('<h',round(1000*math.sin(2*math.pi*spec['parameters']['tone']*i/16000))) for i in range(3200)))
        return output
