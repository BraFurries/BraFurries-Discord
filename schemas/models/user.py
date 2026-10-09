from datetime import datetime
from schemas.models.event import Event

class Warning():
    def __init__(self, date:datetime, reason:str, expired:bool = False):
        self.date:datetime = date
        self.reason:str = reason
        self.expired:bool = expired

    def __str__(self):
        return f'Reason: {self.reason}'

    def __repr__(self):
        return f'Reason: {self.reason}'
    

class Item():
    def __init__(self, item, description, amount):
        self.item = item
        self.description = description
        self.amount = amount

    def __str__(self):
        return f'Item: {self.item}'

    def __repr__(self):
        return f'Item: {self.item}'


class UserNote():
    def __init__(self, note: str, author_discord_id: int | None = None, note_id: int | None = None):
        self.id = note_id
        self.note = note
        self.author_discord_id = author_discord_id

    def __str__(self):
        return self.note

    def __repr__(self):
        return self.note



class User():
    def __init__(
        self,
        displayName,
        discordId: int,
        memberSince: datetime,
        approved=None,
        approvedAt: datetime = None,
        id: int = None,
        username: str = None,
        isVip: bool = False,
        isPartner: bool = False,
        level: int = 0,
        locale=None,
        birthday: datetime = None,
        birthdayVerified: bool = False,
        vipType=None,
        vipSince=None,
        warnings: list[Warning] = [],
        xp=0,
        coins=0,
        inventory: list[Item] = [],
        staffOf: list[Event] = [],
        altAccounts: list[int] | None = None,
        voiceRecord: dict | None = None,
        gameRecord: dict | None = None,
        notes: list[UserNote] | None = None,
        notesCount: int = 0,
    ):
        self.id = id
        self.discordId = discordId
        self.displayName = displayName
        self.isVip = isVip
        self.isPartner = isPartner
        self.vipType = vipType
        self.vipSince = vipSince
        self.memberSince = memberSince
        self.approved = approved
        self.approvedAt = approvedAt
        self.warnings = warnings
        self.birthday = birthday
        self.birthdayVerified = birthdayVerified
        self.locale = locale
        self.level = level if level else 0
        self.xp = xp if xp else 0
        self.coins = coins if coins else 0
        self.inventory = inventory if inventory else []
        self.staffOf = staffOf
        self.username = username
        self.warnings = warnings
        self.altAccounts = altAccounts or []
        self.voiceRecord = voiceRecord
        self.gameRecord = gameRecord
        self.notes = notes or []
        self.notesCount = notesCount if notesCount else len(self.notes)

    def __str__(self):
        return f'Name: {self.name}'
    
    
class CustomRole():
    def __init__(
        self,
        userId,
        color,
        iconId,
        color2=None,
        roleId=None,
        *,
        rowId=None,
        internalUserId=None,
        ownerDiscordUserId=None,
        linkedDiscordUserIds=None,
    ):
        self.userId = userId
        self.color = color
        self.color2 = color2
        self.iconId = iconId
        self.roleId = roleId
        self.rowId = rowId
        self.internalUserId = internalUserId
        self.ownerDiscordUserId = (
            ownerDiscordUserId if ownerDiscordUserId is not None else userId
        )
        self.linkedDiscordUserIds = linkedDiscordUserIds or []
        pass
    

class UserStats():
    def __init__(self, userId, xp, coins, level):
        self.userId = userId
        self.xp = xp
        self.coins = coins
        self.level = level
        pass
    

class SimpleUserBirthday():
    def __init__(self, discordUserId, birthday, userId=None):
        self.DiscordId = discordUserId
        self.userId = userId
        self.birthday = birthday
        pass
