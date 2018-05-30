import math
import logging
import os
import asyncio
import aiohttp
from datetime import datetime, timedelta
import pytz

from aiohttp import web
import json
from db import db, orm, Clan, Province, Front

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
        clans = orm.select(p for p in Clan if p.tag == tag and p.region == region)[:]
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
            print(data)
            for clan in data['data']:
                if clan['tag'] == tag:
                    with orm.db_session:
                        clan = Clan(clan_id=str(clan['clan_id']), tag=clan['tag'], region=region)
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
    for i in clan_provinces or []:
        provinces.append(i['province_id'])

    for i in clan_battles['planned_battles']:
        provinces.append(i['province_id'])

    for i in clan_battles['battles']:
        provinces.append(i['province_id'])

    provinces = list(set(provinces))

    log.debug('Got %s provinces for %s: %s', len(provinces), clan.tag, provinces)
    return provinces


@timeit
async def get_front_name(region, front_id, fronts):
    print(fronts)
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

        if tournament['pretenders']:
            pretenders = [i['tag'] for i in tournament['pretenders']]
        elif tournament['battles']:
            pretenders = []
            for i in tournament['battles']:
                pretenders.append(i['first_competitor']['tag'])
                if i['second_competitor']:
                    pretenders.append(i['second_competitor']['tag'])
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
        } for i in range(rounds)]

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

        for i in battles:
            if i['is_fake']:
                if i['first_competitor']['tag'] == clan.tag:
                    times[round_number - 1]['is_fake'] = True
            elif i['first_competitor']['tag'] == clan.tag or i['second_competitor']['tag'] == clan.tag:
                times[round_number - 1].update({
                    'clan_a': i['first_competitor'],
                    'clan_b': i['second_competitor'],
                })

        if times:
            times = times[round_number - 1:]
            if owner == clan.tag:
                times = [owner_battle]

            all_battles.append({
                'id': tournament['province_id'],
                'region': region,
                'front_name': await get_front_name(region, tournament['front_id'], fronts),
                'province_name': tournament['province_name'],
                'arena_name': tournament['arena_name'],
                'start_time': int(start_time.timestamp() * 1000),
                'prime_time': str(start_time.time()),
                'times': times
            })

    all_battles.sort(key=lambda x: (x['times'][0]['time'], x['start_time'], x['id']))

    return all_battles


async def list_battles(request):
    region = request.match_info.get('region')
    tag = request.match_info.get('tag')
    log.debug('region=%s tag=%s', region, tag)
    clan = await get_clan_info(region, tag)
    data = {}

    if clan:
        provinces = await get_clan_provinces(region, clan)
        battles = await get_clan_battles(region, provinces, clan)

        with orm.db_session:
            for battle in battles:
                province = battle['id']
                p = orm.select(p for p in Province if p.province == province and p.region == region)[:1]
                if p:
                    battle['tags'] = json.loads(p[0].tags)['tags']
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
    province_id = request.match_info.get('province')
    data = json.loads(await request.text())

    with orm.db_session:
        provinces = orm.select(p for p in Province if p.province == province_id and p.region == region)[:1]
        province = provinces[0] if len(provinces) else None

        if province:
            province.tags = json.dumps(data)
        else:
            Province(region=region, province=province_id, tags=json.dumps(data))

    return web.Response(
        text=json.dumps(data, indent=4),
        content_type='application/json',
        headers={'Access-Control-Allow-Origin': '*'}
    )

def main():
    app = web.Application()
    app.router.add_post('/tags/{region}/{province}', set_tags)
    app.router.add_get('/{region}/{tag}', list_battles)
    web.run_app(app)


if __name__ == '__main__':
    main()
