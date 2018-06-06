import traceback
from itertools import chain
import math
import logging
import os
import asyncio
import aiohttp
from datetime import datetime, timedelta
import pytz

from aiohttp import web
import aioredis

from aiohttp_session import setup, get_session
from aiohttp_session.redis_storage import RedisStorage

import json
from pony import orm
from db import Clan, Province, Front, ProvinceTag

from timeit import timeit

WARGAMING_API = os.environ['WARGAMING_API']

logging.basicConfig(format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s')
log = logging.getLogger('wot-proxy-server')
log.setLevel(logging.DEBUG)


def get_papi_url(region, path):
    if region == 'na':
        url = f'https://api.worldoftanks.com/{path}/'
    else:
        url = f'https://api.worldoftanks.{region}/{path}/'
    log.debug('URL generated: "%s"', url)
    return url


async def get_clan_info(region, tag):
    tag = tag.upper()

    with orm.db_session:
        clans = orm.select(p for p in Clan if p.clan_tag == tag and p.region == region)[:]
    if len(clans):
        return clans[0]

    params = {
        'application_id': WARGAMING_API,
        'search': tag,
    }
    async with aiohttp.ClientSession() as session:
        url = get_papi_url(region, 'wgn/clans/list')
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            for clan in data['data']:
                if clan['tag'] == tag:
                    with orm.db_session:
                        clan = Clan(clan_id=str(clan['clan_id']), clan_tag=clan['tag'], region=region)
                    return clan
            return None


class ClanBattles:
    def __init__(self, region: str, clan: Clan):
        self.region = region
        self.clan = clan

    def get_papi_url(self, path):
        if self.region == 'na':
            url = f'https://api.worldoftanks.com/{path}/'
        else:
            url = f'https://api.worldoftanks.{self.region}/{path}/'
        log.debug('URL generated: "%s"', url)
        return url

    def get_game_api_url(self, path):
        url = f'https://{self.region}.wargaming.net/globalmap/game_api/{path}'
        log.debug('URL generated: "%s"', url)
        return url

    @timeit
    async def get_game_api_battles(self):
        clan_id = self.clan.clan_id
        url = self.get_game_api_url(f'clan/{clan_id}/battles')
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                # debug output
                open(f'dumps/clan_{clan_id}_battles.json', 'w').write(json.dumps(data, indent=4))
                return data

    @timeit
    async def get_papi_clan_provinces(self):
        params = {
            'application_id': WARGAMING_API,
            'clan_id': self.clan.clan_id,
        }
        url = self.get_papi_url('wot/globalmap/clanprovinces')
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                return data

    @timeit
    async def get_papi_fronts(self):
        params = {
            'application_id': WARGAMING_API,
        }
        url = self.get_papi_url('wot/globalmap/fronts')
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                return data


    @timeit
    async def get_papi_provinces(self, front_id, provinces):
        params = {
            'application_id': WARGAMING_API,
            'front_id': front_id,
            'province_id': ','.join(provinces),
            'fields': ','.join(['province_id', 'province_name', 'server', 'front_id'])
        }
        url = self.get_papi_url('wot/globalmap/provinces')
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                return data['data']


    @timeit
    async def get_papi_eventclaninfo(self, clan_id, event_id='arms_race', front_id='arms_race_bg'):
        params = {
            'application_id': WARGAMING_API,
            'event_id': event_id,
            'front_id': front_id,
            'clan_id': clan_id,
        }
        url = self.get_papi_url('wot/globalmap/eventclaninfo')

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                try:
                    data = await resp.json()
                    data = data['data'][str(clan_id)]['events'][event_id][0]
                    data['clan_id'] = clan_id
                    return data
                except:
                    return {clan_id: {}}

    async def get_tournament_info(self, province_id, round_number=None):
        url = f'https://{self.region}.wargaming.net/globalmap/game_api/tournament_info'
        params = {
            'alias': province_id
        }
        if round_number:
            params['round'] = round_number
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                data_round_number = data.get('round_number', 'ERR')
                filename = f'dumps/tournament_info_{province_id}_round_{data_round_number}.json'
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(json.dumps(data, indent=4))
                return data


    @timeit
    async def get_clan_provinces(self):
        tasks = [
            self.get_game_api_battles(),
            self.get_papi_clan_provinces()
        ]
        provinces = await asyncio.gather(*tasks)
        clan_battles = provinces[0]
        clan_provinces = provinces[1]['data'][str(self.clan.clan_id)] or []

        provinces = []
        front_provinces = {}
        for i in clan_provinces or []:
            provinces.append(i['province_id'])
            front_provinces.setdefault(i['front_id'], []).append(i['province_id'])

        for i in clan_battles['planned_battles']:
            provinces.append(i['province_id'])
            front_provinces.setdefault(i['front_id'], []).append(i['province_id'])

        for i in clan_battles['battles']:
            provinces.append(i['province_id'])
            front_provinces.setdefault(i['front_id'], []).append(i['province_id'])

        # making list unique
        provinces = list(set(provinces))
        for k, v in front_provinces.items():
            front_provinces[k] = list(set(v))

        log.debug('Got %s provinces for %s: %s', len(provinces), self.clan.clan_tag, provinces)
        return provinces, front_provinces, clan_provinces


    async def get_front_name(self, front_id, fronts):
        if front_id not in fronts:
            with orm.db_session:
                for front in (await self.get_papi_fronts())['data']:
                    if front['front_id'] not in fronts:
                        Front(front_id=front['front_id'], front_name=front['front_name'])
                        fronts[front['front_id']] = front['front_name']

        return fronts[front_id]


    async def parse_tournament(self, tournament, fronts):
        clan = self.clan
        now = datetime.now(tz=pytz.UTC).replace(microsecond=0)
        province_id = tournament['province_id']
        start = [int(i) for i in tournament['start_time'].split(':')]
        start_time = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
        battles = tournament['battles']
        round_number = tournament['round_number']
        next_round = tournament['next_round']
        owner = tournament['owner'] and tournament['owner']['tag']

        # ignore tournament if last round list already finished
        if len(battles) == 1 and owner == battles[0]['first_competitor']['tag'] and battles[0]['winner_id']:
            return

        clans = {i['tag']: i for i in tournament['pretenders'] or []}
        clans.update({
            i['first_competitor']['tag']: i['first_competitor']
            for i in tournament['battles']
        })
        clans.update({
            i['second_competitor']['tag']: i['second_competitor']
            for i in tournament['battles'] if i['second_competitor']
        })
        if owner:
            clans[owner] = tournament['owner']

        # now + timedelta(hours=1) because battles are formed 1 hour before starting battles
        if start_time.hour > (now + timedelta(hours=1)).hour and battles:
            start_time -= timedelta(days=1)
        elif start_time.hour <= now.hour and not battles:
            start_time += timedelta(days=1)

        # print('>> %-13s (%2d) %s %s' % (tournament['province_id'], len(battles), start_time, now))

        rounds = round_number - 1

        next_round_pretenders = []

        if tournament['pretenders']:
            pretenders = [i['tag'] for i in tournament['pretenders']]
            next_round_pretenders = tournament['pretenders']
        elif battles:
            pretenders = []
            for battle in battles:
                first_competitor = battle['first_competitor']
                second_competitor = battle['second_competitor']
                pretenders.append(first_competitor['tag'])

                if battle['is_fake']:
                    next_round_pretenders.append(first_competitor)
                else:
                    pretenders.append(battle['second_competitor']['tag'])

                    winner_id = battle['winner_id']
                    if winner_id:
                        next_round_pretenders.append(
                            first_competitor if winner_id == first_competitor['id'] else second_competitor
                        )
        else:
            pretenders = []

        log.debug('%-20s | owner : %5s | pretenders : %s', tournament['province_id'], owner, pretenders)
        if pretenders:
            rounds += math.ceil(math.log2(len(pretenders)))

        if owner in pretenders:
            rounds -= 1

        times = [{
            'winner_id': None,
            'is_fake': False,
            'title': 'Final' if (i + 1 == rounds) else f'1/{pow(2, rounds-i-1)}',
            'time': int((start_time + timedelta(minutes=30) * i).timestamp() * 1000),
            'duration': 1800000,
            'clan_a': None,
            'clan_b': None,
            'pretenders': next_round_pretenders if i + 1 == next_round and next_round_pretenders else None
        } for i in range(rounds)]

        next_round_pretenders.sort(key=lambda x: x['elo_rating_10'])

        # special case
        owner_battle = {
            'winner_id': None,
            'title': 'Owner',
            'time': int((start_time + timedelta(minutes=30) * len(times)).timestamp() * 1000),
            'duration': 1800000,
            'clan_a': tournament['owner'],
            'clan_b': None,
        }

        if owner and pretenders:
            times.append(owner_battle)

        for battle in battles:
            first_competitor = battle['first_competitor']
            second_competitor = battle['second_competitor']

            province_round = times[round_number - 1]

            if battle['is_fake']:
                if first_competitor['tag'] == clan.clan_tag:
                    province_round.update({
                        'is_fake': True,
                        'winner_id': battle['winner_id'],
                    })
            elif first_competitor['tag'] == clan.clan_tag or second_competitor['tag'] == clan.clan_tag:
                province_round.update({
                    'winner_id': battle['winner_id'],
                    'clan_a': battle['first_competitor'],
                    'clan_b': battle['second_competitor'],
                })

        # filter out already passed rounds
        if times:
            # remove rounds before current round
            times = times[round_number - 1:]

            # if winner already determined for current round
            if times[0]['winner_id']:
                times = times[1:]

        if times:
            if owner == clan.clan_tag:
                times = [owner_battle]
            elif round_number == 1 and not battles:
                times[0]['pretenders'] = tournament['pretenders']
            else:
                if next_round_pretenders:
                    while next_round_pretenders:
                        next_round_pretenders.pop()
                    next_battle = await self.get_tournament_info(province_id, next_round)
                    next_round_pretenders.extend(next_battle['pretenders'])

            return {
                'id': tournament['province_id'],
                'region': self.region,
                'front_name': await self.get_front_name(tournament['front_id'], fronts),
                'province_name': tournament['province_name'],
                'arena_name': tournament['arena_name'],
                'start_time': int(start_time.timestamp() * 1000),
                'prime_time': str(start_time.time()),
                'times': times,
                'pretenders': pretenders,
            }


    @timeit
    async def get_clan_battles(self, provinces):
        tournaments = await asyncio.gather(*[self.get_tournament_info(p) for p in provinces])

        with orm.db_session:
            fronts = {f.front_id: f.front_name for f in orm.select(f for f in Front)}

        async def get_tournament_task(tournament):
            try:
                data = await self.parse_tournament(tournament=tournament, fronts=fronts)
                return [data] if data else []
            except:
                now = datetime.now().isoformat()
                province_id = tournament.get('province_id', 'NO-PROVINCE-ID')
                filename = f'errors/province/{now}_{province_id}'
                with open(f'{filename}.json', 'w', encoding='utf-8') as f:
                    f.write(json.dumps(tournament, indent=4))
                with open(f'{filename}.traceback', 'w', encoding='utf-8') as f:
                    f.write(traceback.format_exc())
                log.error("Unable to parse province: '%s'", province_id)
            return []

        all_battles = list(chain(*await asyncio.gather(*[get_tournament_task(i) for i in tournaments])))

        try:
            all_battles.sort(key=lambda x: (x['times'][0]['time'], x['start_time'], x['id']))
        except:
            now = datetime.now().isoformat()
            filename = f'errors/province/{now}_sort_all_battles'
            with open(f'{filename}.json', 'w', encoding='utf-8') as f:
                f.write(json.dumps(all_battles, indent=4))
            with open(f'{filename}.traceback', 'w', encoding='utf-8') as f:
                f.write(traceback.format_exc())
            log.error("Unable to sort")

        # collect clans needed extra data
        need_clan_event_info = set()
        for battle in all_battles:
            next_battle = battle['times'][0]
            if next_battle['clan_a']:
                need_clan_event_info.add(next_battle['clan_a']['id'])
            if next_battle['clan_b']:
                need_clan_event_info.add(next_battle['clan_b']['id'])

        clan_info = {}
        for data in await asyncio.gather(*[self.get_papi_eventclaninfo(i) for i in need_clan_event_info]):
            clan_info[data['clan_id']] = data

        # append clan info to battles
        for battle in all_battles:
            next_battle = battle['times'][0]
            if next_battle['clan_a']:
                clan_id = next_battle['clan_a']['id']
                next_battle['clan_a']['fame_points'] = clan_info[clan_id]['fame_points']
            if next_battle['clan_b']:
                clan_id = next_battle['clan_b']['id']
                next_battle['clan_b']['fame_points'] = clan_info[clan_id]['fame_points']

        return all_battles


    async def get_provinces_data(self, front_provinces):
        all_provinces = list(chain(*map(lambda x: x, front_provinces.values())))

        with orm.db_session:
            provinces_data = {
                p.province_id: p
                for p in orm.select(p for p in Province if p.province_id in all_provinces)
            }

        missing_task = []
        missing = {}

        for front_id, provinces in front_provinces.items():
            for province_id in provinces:
                if province_id not in provinces_data:
                    missing.setdefault(front_id, []).append(province_id)

        for front_id, provinces in missing.items():
            missing_task.append(self.get_papi_provinces(front_id, provinces))

        for p in chain(*await asyncio.gather(*missing_task)):
            with orm.db_session:
                provinces_data[p['province_id']] = Province(
                    region=self.region,
                    province_name=p['province_name'],
                    front_id=p['front_id'],
                    province_id=p['province_id'],
                    server=p['server'],
                )

        return provinces_data

    async def list_battles(self):
        clan = self.clan

        # get provinces list on which clan can have battles
        all_provinces, front_provinces, clan_provinces = await self.get_clan_provinces()

        # get provinces extra data from cache or papi -> server
        provinces = await self.get_provinces_data(front_provinces)

        # get battles info from game_api/tournament_info
        battles = await self.get_clan_battles(all_provinces)

        with orm.db_session:
            for battle in battles:
                province = provinces[battle['id']]
                battle['server'] = province.server
                tags = ProvinceTag.select(lambda tags: tags.province == province and tags.clan == clan)[:1]
                if tags:
                    battle['tags'] = json.loads(tags[0].tags)['tags']
                else:
                    battle['tags'] = []

        data = {'tag': clan.clan_tag, 'clan_id': clan.clan_id, 'items': battles, 'clan_provinces': clan_provinces}
        return data


async def list_battles(request):
    region = request.match_info.get('region')
    tag = request.match_info.get('tag')
    log.debug('region=%s tag=%s', region, tag)
    clan = await get_clan_info(region, tag)
    data = {}

    if clan:
        data = await ClanBattles(region, clan).list_battles()

    return web.Response(
        text=json.dumps(data, indent=4),
        content_type='application/json',
        headers={'Access-Control-Allow-Origin': '*'}
    )

async def set_tags(request):
    region = request.match_info.get('region')
    clan_id = int(request.match_info.get('clan_id'))
    province_id = request.match_info.get('province_id')
    data = json.loads(await request.text())

    with orm.db_session:
        provinces = Province.select(lambda p: p.province_id == province_id and p.region == region)[:1]
        province = provinces[0] if len(provinces) else None

        clans = Clan.select(lambda c: c.clan_id == clan_id)[:1]
        clan = clans[0] if len(clans) else None


        if province and clan:
            tags = ProvinceTag.select(lambda tags: tags.province == province and tags.clan == clan)[:1]
            if tags:
                tags[0].tags = json.dumps(data)
            else:
                ProvinceTag(province=province, clan=clan, tags=json.dumps(data))

    return web.Response(
        text=json.dumps(data, indent=4),
        content_type='application/json',
        headers={'Access-Control-Allow-Origin': '*'}
    )


async def make_redis_pool():
    redis_address = ('127.0.0.1', '6379')
    return await aioredis.create_redis_pool(redis_address, timeout=1)


def main():
    loop = asyncio.get_event_loop()
    redis_pool = loop.run_until_complete(make_redis_pool())
    storage = RedisStorage(redis_pool)

    async def dispose_redis_pool(app):
        redis_pool.close()
        await redis_pool.wait_closed()

    app = web.Application()
    setup(app, storage)
    app.on_cleanup.append(dispose_redis_pool)

    app.router.add_post('/tags/{region}/{clan_id}/{province_id}', set_tags)
    app.router.add_get('/{region}/{tag}', list_battles)
    web.run_app(app)


if __name__ == '__main__':
    if not os.path.exists('errors'):
        os.mkdir('errors')
    if not os.path.exists('errors/province'):
        os.mkdir('errors/province')
    if not os.path.exists('errors/other'):
        os.mkdir('errors/other')
    main()
