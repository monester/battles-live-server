from pony import orm

db = orm.Database()


class Clan(db.Entity):
    clan_id = orm.Required(str)
    tag = orm.Required(str)
    region = orm.Required(str)

class Province(db.Entity):
    region = orm.Required(str)
    province = orm.Required(str)
    tags = orm.Optional(str)


db.bind(provider='sqlite', filename='db.sqlite', create_db=True)
db.generate_mapping(create_tables=True)
# orm.set_sql_debug(True)
