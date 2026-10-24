"""Independent synthetic image package, without inference or a QQ sender."""
from pathlib import Path
import time


class Adapter:
    def describe(self,config):
        return dict(protocol='media-adapter/1',engine='fixture-image',kinds=['image'],resource='gpu:0',
            summary='Synthetic colored image for extension verification.',operations=['generate','reference_edit'],
            reference_limit=1,mask=False,parameters_schema=dict(type='object',properties=dict(
                color=dict(type='string',enum=['blue','red'])),additionalProperties=False),
            output=dict(kind='image',mime='image/png',extension='.png'),cancel_grace_seconds=0)

    def prepare(self,p,assets,config,voice=None):
        from life.agent_session import SessionFailure
        width,height=p.get('width',256),p.get('height',256)
        if any(type(v) is not int or not 64<=v<=1024 for v in (width,height)):
            raise SessionFailure('invalid_input','Synthetic dimensions must be integers in 64..1024')
        return dict(kind='image',prompt=p.get('prompt',''),seed=p.get('seed'),width=width,height=height,
            references=assets['references'],color=p.get('provider_options',{}).get('color','blue'))

    def execute(self,spec,context):
        from PIL import Image
        until=time.monotonic()+context.config.get('delay',0)
        while time.monotonic()<until:
            context.check_cancelled();time.sleep(.03)
        output=context.folder/'output.png'
        Image.new('RGB',(spec['width'],spec['height']),spec['color']).save(output)
        return output
