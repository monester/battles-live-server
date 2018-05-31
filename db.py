import os
from pony.orm import *


db = Database()


class Clan(db.Entity):
    clan_id = PrimaryKey(int, sql_type='BIGINT')
    clan_tag = Optional(str)
    region = Optional(str)
    province_tags = Set('ProvinceTag')


class Province(db.Entity):
    id = PrimaryKey(int, auto=True)
    region = Required(str, 4)
    front_id = Required(str)
    province_id = Required(str, 255)
    province_name = Optional(str)
    server = Required(str)
    tags = Set('ProvinceTag')


class ProvinceTag(db.Entity):
    id = PrimaryKey(int, auto=True)
    clan = Required(Clan, column='clan_id')
    province = Required(Province, column='province_id')
    tags = Optional(str)


class Front(db.Entity):
    front_id = PrimaryKey(str, 255)
    front_name = Optional(str)


db.bind(provider='sqlite', filename='db.sqlite', create_db=True)

db.generate_mapping(create_tables=True)
