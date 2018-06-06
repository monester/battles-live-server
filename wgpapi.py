import aiohttp
import logging

log = logging.getLogger('wot-proxy-server')


class PAPIException(Exception):
    def __init__(self, message, code, field=None, value=None, **kwargs):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field
        self.value = value

    def __repr__(self):
        return f'{self.__class__.__name__}: {self.code} - {self.message}'


class PAPI:
    def __init__(self, region, application_id, language=None):
        self.region = region
        self.application_id = application_id
        self.language = language

    def get_url(self, method):
        method = method.strip('/')
        if self.region == 'na':
            url = f'https://api.worldoftanks.com/{method}/'
        else:
            url = f'https://api.worldoftanks.{self.region}/{method}/'
        log.debug('URL generated: "%s"', url)
        return url

    async def __call__(self, method, params=None):
        params = params or {}
        params.update({
            'application_id': self.application_id
        })

        if self.language:
            params.update({
                'language': self.language
            })

        url = self.get_url(method)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                if data['status'] == 'ok':
                    return data['data']
                else:
                    raise PAPIException(**data['error'])
