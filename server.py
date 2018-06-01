from itertools import chain
import math
import logging
import os
import asyncio
import aiohttp
from datetime import datetime, timedelta
import pytz

from aiohttp import web
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


def get_game_api_url(region, path):
    url = f'https://{region}.wargaming.net/globalmap/game_api/{path}'
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


@timeit
async def get_game_api_battles(region, clan_id):
    url = get_game_api_url(region, f'clan/{clan_id}/battles')
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            # debug output
            open(f'dumps/clan_{clan_id}_battles.json', 'w').write(json.dumps(data, indent=4))
            return data


@timeit
async def get_papi_clan_provinces(region, clan_id):
    params = {
        'application_id': WARGAMING_API,
        'clan_id': clan_id,
    }
    url = get_papi_url(region, 'wot/globalmap/clanprovinces')
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            return data


@timeit
async def get_papi_fronts(region):
    params = {
        'application_id': WARGAMING_API,
    }
    url = get_papi_url(region, 'wot/globalmap/fronts')
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            return data

@timeit
async def get_papi_provinces(region, front_id, provinces):
    params = {
        'application_id': WARGAMING_API,
        'front_id': front_id,
        'province_id': ','.join(provinces),
        'fields': ','.join(['province_id', 'province_name', 'server', 'front_id'])
    }
    url = get_papi_url(region, 'wot/globalmap/provinces')
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            return data['data']


async def get_tournament_info(region, province_id):
    url = f'https://{region}.wargaming.net/globalmap/game_api/tournament_info?alias={province_id}'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            open(f'dumps/tournament_info_{province_id}.json', 'w').write(json.dumps(data, indent=4))
            return data


@timeit
async def get_clan_provinces(region, clan):
    tasks = [
        get_game_api_battles(region, clan.clan_id),
        get_papi_clan_provinces(region, clan.clan_id)
    ]
    provinces = await asyncio.gather(*tasks)
    clan_battles = provinces[0]
    clan_provinces = provinces[1]['data'][str(clan.clan_id)]

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

    log.debug('Got %s provinces for %s: %s', len(provinces), clan.clan_tag, provinces)
    return provinces, front_provinces


async def get_front_name(region, front_id, fronts):
    if front_id not in fronts:
        with orm.db_session:
            for front in (await get_papi_fronts(region))['data']:
                if front['front_id'] not in fronts:
                    Front(front_id=front['front_id'], front_name=front['front_name'])
                    fronts[front['front_id']] = front['front_name']

    return fronts[front_id]


@timeit
async def get_clan_battles(region, provinces, clan):
    tournaments = await asyncio.gather(*[get_tournament_info(region, p) for p in provinces])
    all_battles = []

    with orm.db_session:
        fronts = {f.front_id: f.front_name for f in orm.select(f for f in Front)}

    now = datetime.now(tz=pytz.UTC).replace(microsecond=0)
    for tournament in tournaments:
        start = [int(i) for i in tournament['start_time'].split(':')]
        start_time = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
        battles = tournament['battles']
        round_number = tournament['round_number']
        next_round = tournament['next_round']
        owner = tournament['owner'] and tournament['owner']['tag']

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
            'is_fake': False,
            'title': 'Final' if (i + 1 == rounds) else f'1/{pow(2, rounds-i-1)}',
            'time': int((start_time + timedelta(minutes=30)*i).timestamp() * 1000),
            'duration': 1800000,
            'clan_a': None,
            'clan_b': None,
            'pretenders': next_round_pretenders if i + 1 == next_round and next_round_pretenders else None
        } for i in range(rounds)]

        next_round_pretenders.sort(key=lambda x: x['elo_rating_10'])

        # special case
        owner_battle = {
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

            if battle['is_fake']:
                if first_competitor['tag'] == clan.clan_tag:
                    times[round_number - 1]['is_fake'] = True
            elif first_competitor['tag'] == clan.clan_tag or second_competitor['tag'] == clan.clan_tag:
                times[round_number - 1].update({
                    'clan_a': battle['first_competitor'],
                    'clan_b': battle['second_competitor'],
                })

        if times:
            times = times[round_number - 1:]

            if owner == clan.clan_tag:
                times = [owner_battle]

            all_battles.append({
                'id': tournament['province_id'],
                'region': region,
                'front_name': await get_front_name(region, tournament['front_id'], fronts),
                'province_name': tournament['province_name'],
                'arena_name': tournament['arena_name'],
                'start_time': int(start_time.timestamp() * 1000),
                'prime_time': str(start_time.time()),
                'times': times,
                'pretenders': pretenders,
            })

    all_battles.sort(key=lambda x: (x['times'][0]['time'], x['start_time'], x['id']))

    return all_battles


async def get_provinces_data(region, front_provinces):
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
        missing_task.append(get_papi_provinces(region, front_id, provinces))

    for p in chain(*await asyncio.gather(*missing_task)):
        with orm.db_session:
            provinces_data[p['province_id']] = Province(
                region=region,
                province_name=p['province_name'],
                front_id=p['front_id'],
                province_id=p['province_id'],
                server=p['server'],
            )

    return provinces_data

async def list_battles(request):
    region = request.match_info.get('region')
    tag = request.match_info.get('tag')
    log.debug('region=%s tag=%s', region, tag)
    clan = await get_clan_info(region, tag)
    data = {}

    if clan:
        all_provinces, front_provinces = await get_clan_provinces(region, clan)
        provinces = await get_provinces_data(region, front_provinces)
        battles = await get_clan_battles(region, all_provinces, clan)

        with orm.db_session:
            for battle in battles:
                province = provinces[battle['id']]
                battle['server'] = province.server
                tags = ProvinceTag.select(lambda tags: tags.province == province and tags.clan == clan)[:1]
                if tags:
                    battle['tags'] = json.loads(tags[0].tags)['tags']
                else:
                    battle['tags'] = []

        data = {'tag': tag, 'clan_id': clan.clan_id, 'items': battles}
        # print(json.dumps(battles, indent=4))

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
            print(data)
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

def main():
    app = web.Application()
    app.router.add_post('/tags/{region}/{clan_id}/{province_id}', set_tags)
    app.router.add_get('/{region}/{tag}', list_battles)
    web.run_app(app)


if __name__ == '__main__':
    main()
