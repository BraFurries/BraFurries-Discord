from __future__ import annotations

import asyncio
import weakref
from typing import Any

import discord


_portaria_publication_locks: weakref.WeakValueDictionary[tuple[int, int, int], asyncio.Lock] = weakref.WeakValueDictionary()
_PORTARIA_DEFAULT_BUTTON_LABEL = "Abrir formulário"


def _portaria_publication_message(flow: dict[str, Any], request: dict[str, Any]) -> str:
    if "message" in request:
        message = request["message"]
        if not isinstance(message, str) or len(message) > 2000:
            raise ValueError("invalid_portaria_publication")
        return message
    return f"📋 **{flow['name']}**\nClique no botão para abrir o formulário."


def _portaria_button_label(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_portaria_publication")
    label = value.strip()
    if not label or len(label) > 80:
        raise ValueError("invalid_portaria_publication")
    return label


def _published_portaria_button_label(message: discord.Message, flow_id: int) -> str | None:
    """Return a persisted button label only when its component can be identified safely."""
    expected_custom_id = f"form_flow:{flow_id}"
    labels: list[str] = []
    for row in getattr(message, "components", ()) or ():
        for component in getattr(row, "children", None) or getattr(row, "components", ()) or ():
            if getattr(component, "custom_id", None) != expected_custom_id:
                continue
            label = getattr(component, "label", None)
            if isinstance(label, str) and label.strip() and len(label) <= 80:
                labels.append(label)
    return labels[0] if len(labels) == 1 else None


def can_manage_guild(guild: discord.Guild, user_id: int) -> bool:
    member = guild.get_member(user_id)
    if member is None:
        return False
    permissions = member.guild_permissions
    return (
        guild.owner_id == user_id
        or bool(getattr(permissions, "administrator", False))
        or bool(getattr(permissions, "manage_guild", False))
    )


def build_guild_resources(guild: discord.Guild) -> dict[str, Any]:
    bot_member = guild.me
    bot_permissions = getattr(bot_member, "guild_permissions", None)
    can_manage_channels = bool(getattr(bot_permissions, "manage_channels", False))
    can_manage_roles = bool(getattr(bot_permissions, "manage_roles", False))
    has_administrator = bool(getattr(bot_permissions, "administrator", False))
    bot_top_role = getattr(bot_member, "top_role", None)
    bot_top_position = getattr(bot_top_role, "position", -1)

    # Role's native comparison carries discord.py's hierarchy semantics (including
    # its tie-breaking rules). Administrator does not bypass that hierarchy.
    roles_above_bot = [
        role for role in guild.roles
        if role != guild.default_role
        and role != bot_top_role
        and not (bot_top_role > role)
    ] if bot_top_role is not None else []
    role_hierarchy_ok = not roles_above_bot

    roles = sorted(
        (role for role in guild.roles if role != guild.default_role),
        reverse=True,
    )
    role_resources = [
        {
            "id": str(role.id),
            "name": role.name,
            "position": role.position,
            "managed": role.managed,
            "editableByBot": (
                can_manage_roles
                and not role.managed
                and role != guild.default_role
                and role.position < bot_top_position
            ),
        }
        for role in roles
    ]
    # discord.py guarantees by_category() follows the official Discord UI
    # order, including uncategorized channels first and channels within each
    # category in their displayed order. Preserve that order end-to-end so
    # every consumer of GuildResources sees the same sequence as Discord.
    channel_groups = guild.by_category()
    ordered_channels = [
        channel
        for _category, grouped_channels in channel_groups
        for channel in grouped_channels
    ]
    channels = [
        {
            "id": str(channel.id),
            "name": channel.name,
            "type": str(getattr(channel, "type", "channel")),
            "categoryId": str(channel.category_id) if getattr(channel, "category_id", None) else None,
        }
        for channel in ordered_channels
    ]
    # Keep categories sourced from guild.categories so empty categories remain
    # visible in the resource payload. Channel ordering is the only behavior
    # intentionally delegated to by_category().
    categories = [
        {"id": str(category.id), "name": category.name}
        for category in guild.categories
    ]
    return {
        "guildId": str(guild.id),
        "roles": role_resources,
        "channels": channels,
        "categories": categories,
        "botCapabilities": {
            "canManageChannels": can_manage_channels,
            "canManageRoles": can_manage_roles,
            "canApply": can_manage_channels and can_manage_roles,
            "missingPermissions": [
                permission
                for permission, available in (
                    ("MANAGE_CHANNELS", can_manage_channels),
                    ("MANAGE_ROLES", can_manage_roles),
                )
                if not available
            ],
            "hasAdministrator": has_administrator,
            "roleHierarchyOk": role_hierarchy_ok,
            "protectionReady": has_administrator and role_hierarchy_ok,
            "botTopRole": {
                "id": str(bot_top_role.id),
                "name": bot_top_role.name,
                "position": bot_top_role.position,
            } if bot_top_role is not None else None,
            "rolesAboveBot": [
                {
                    "id": str(role.id),
                    "name": role.name,
                    "position": role.position,
                    "managed": role.managed,
                }
                for role in sorted(roles_above_bot, reverse=True)
            ],
        },
    }


def build_structure_preview(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    resources = build_guild_resources(guild)
    preview_type = request.get("type")
    if preview_type == "portaria":
        preview = _build_portaria_preview(guild, request)
    elif preview_type == "private-area":
        preview = _build_private_area_preview(request)
    else:
        raise ValueError("unsupported_preview_type")

    preview["botCapabilities"] = resources["botCapabilities"]
    preview["requiredBotPermissions"] = ["MANAGE_CHANNELS", "MANAGE_ROLES"]
    preview["warnings"] = _role_hierarchy_warnings(resources["roles"], request)
    return preview


def _build_portaria_preview(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    role_name = _clean_name(request.get("visitorRoleName"), "Visitante")
    category_name = _clean_name(request.get("categoryName"), "Portaria")
    channel_names = _clean_names(request.get("channelNames"), ["boas-vindas", "fichas", "aprovados", "reprovados"])
    role_ref = {"kind": "planned-role", "name": role_name}
    staff_roles = [{"kind": "role", "id": str(role_id)} for role_id in request.get("staffRoleIds", [])]
    permission_changes = []
    for index, channel_name in enumerate(channel_names):
        target = {"kind": "planned-channel", "name": channel_name}
        permission_changes.append({
            "target": target,
            "subject": {"kind": "everyone"},
            "permission": "VIEW_CHANNEL",
            "effect": "deny",
        })
        permission_changes.append({
            "target": target,
            "subject": role_ref,
            "permission": "VIEW_CHANNEL",
            "effect": "allow" if index == 0 else "deny",
        })
        for staff_role in staff_roles:
            permission_changes.append({
                "target": target,
                "subject": staff_role,
                "permission": "VIEW_CHANNEL",
                "effect": "allow",
            })

    affected_existing = []
    if bool(request.get("isolateVisitors")):
        affected_existing = _isolation_targets(guild)
        permission_changes.extend({
            "target": {"kind": item["type"], "id": item["id"], "name": item["name"]},
            "subject": role_ref,
            "permission": "VIEW_CHANNEL",
            "effect": "deny",
        } for item in affected_existing)

    return {
        "type": "portaria",
        "creates": {
            "roles": [{"name": role_name}],
            "categories": [{"name": category_name}],
            "channels": [{"name": name, "type": "text", "categoryName": category_name} for name in channel_names],
        },
        "permissionChanges": permission_changes,
        "affectedExistingResources": affected_existing,
    }


def _isolation_targets(guild: discord.Guild) -> list[dict[str, str]]:
    """Return every existing channel target once, including uncategorized channels."""
    targets: dict[str, dict[str, str]] = {}
    for channel in getattr(guild, "channels", []):
        channel_id = str(channel.id)
        channel_type = str(getattr(channel, "type", "channel"))
        targets[channel_id] = {
            "id": channel_id,
            "name": channel.name,
            "type": "category" if channel_type == "category" else channel_type,
        }
    for channel in getattr(guild, "text_channels", []):
        targets.setdefault(str(channel.id), {"id": str(channel.id), "name": channel.name, "type": "text"})
    for category in getattr(guild, "categories", []):
        targets.setdefault(str(category.id), {"id": str(category.id), "name": category.name, "type": "category"})
    return list(targets.values())


def _build_private_area_preview(request: dict[str, Any]) -> dict[str, Any]:
    category_name = _clean_name(request.get("categoryName"), "Área privada")
    channel_names = _clean_names(request.get("channelNames"), ["privado"])
    role_ids = [str(role_id) for role_id in request.get("allowedRoleIds", [])]
    permission_changes = []
    for channel_name in channel_names:
        target = {"kind": "planned-channel", "name": channel_name}
        permission_changes.append({
            "target": target,
            "subject": {"kind": "everyone"},
            "permission": "VIEW_CHANNEL",
            "effect": "deny",
        })
        permission_changes.extend({
            "target": target,
            "subject": {"kind": "role", "id": role_id},
            "permission": "VIEW_CHANNEL",
            "effect": "allow",
        } for role_id in role_ids)
    return {
        "type": "private-area",
        "creates": {
            "roles": [],
            "categories": [{"name": category_name}],
            "channels": [{"name": name, "type": "text", "categoryName": category_name} for name in channel_names],
        },
        "permissionChanges": permission_changes,
        "affectedExistingResources": [],
    }


def _role_hierarchy_warnings(roles: list[dict[str, Any]], request: dict[str, Any]) -> list[str]:
    selected_ids = {
        str(role_id)
        for key in ("staffRoleIds", "allowedRoleIds")
        for role_id in request.get(key, [])
    }
    return [
        f"O cargo {role['name']} está acima do cargo do Coddy ou não pode ser editado."
        for role in roles
        if role["id"] in selected_ids and not role["editableByBot"]
    ]


def _clean_name(value: Any, fallback: str) -> str:
    normalized = str(value or "").strip()
    return normalized or fallback


def _clean_names(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return fallback
    names = [str(item).strip() for item in value if str(item).strip()]
    return names or fallback


async def apply_guild_operation(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    """Apply a bounded Discord operation using the guild state observed now.

    This deliberately keeps no ownership ledger: exact-name reconciliation is
    used only when it has a single candidate; ambiguous legacy state is a
    conflict rather than an unsafe guess.
    """
    operation = request.get("operation")
    permissions = getattr(getattr(guild, "me", None), "guild_permissions", None)
    needs_roles = operation in {"portaria-template", "portaria-repair-roles", "portaria-create-role", "vip-reconcile"}
    if needs_roles and not bool(getattr(permissions, "manage_roles", False)):
        raise PermissionError("missing_manage_roles")
    if operation in {"portaria-template", "private-area", "visitor-isolation", "portaria-permissions", "portaria-repair-structure", "portaria-publish", "portaria-create-channel"} and not bool(getattr(permissions, "manage_channels", False)):
        raise PermissionError("missing_manage_channels")
    if operation == "portaria-template":
        return await _apply_portaria_template(guild, request)
    if operation == "private-area":
        return await _apply_private_area(guild, request)
    if operation == "visitor-isolation":
        return await _apply_visitor_isolation(guild, request)
    if operation == "portaria-permissions":
        return await _apply_portaria_permissions(guild, request)
    if operation == "portaria-repair-roles":
        return await _apply_portaria_repair_roles(guild, request)
    if operation == "portaria-repair-structure":
        return await _apply_portaria_repair_structure(guild, request)
    if operation == "portaria-publish":
        return await _publish_portaria_form(guild, request)
    if operation == "portaria-create-role":
        return await _create_portaria_role(guild, request)
    if operation == "portaria-create-channel":
        return await _create_portaria_channel(guild, request)
    if operation == "portaria-delete-publications":
        return await _delete_portaria_publications(guild, request)
    if operation == "vip-reconcile":
        from core.routine_functions import reconcileVipCustomRoles
        return await reconcileVipCustomRoles(guild)
    if operation == "portaria-bypass-validate":
        return await _validate_portaria_bypass_target(guild, request)
    raise ValueError("unsupported_operation")


def _single(items: list[Any], name: str) -> Any | None:
    matches = [item for item in items if getattr(item, "name", "").casefold() == name.casefold()]
    if len(matches) > 1:
        raise RuntimeError(f"ambiguous_resource:{name}")
    return matches[0] if matches else None


async def _ensure_role(guild: discord.Guild, name: str) -> tuple[Any, str]:
    if name.strip().casefold() in {"@everyone", "everyone"}:
        raise ValueError("invalid_visitor_role_name")
    role = _single([role for role in guild.roles if role != guild.default_role and not getattr(role, "managed", False)], name)
    return (role, "reused") if role else (await guild.create_role(name=name, reason="Portaria Coddy"), "created")


async def _ensure_category(guild: discord.Guild, name: str) -> tuple[Any, str]:
    category = _single(list(guild.categories), name)
    return (category, "reused") if category else (await guild.create_category(name=name, reason="Portaria Coddy"), "created")


async def _ensure_text_channel(guild: discord.Guild, category: Any, name: str) -> tuple[Any, str]:
    candidates = [channel for channel in guild.text_channels if channel.name.casefold() == name.casefold() and channel.category_id == category.id]
    if len(candidates) > 1:
        raise RuntimeError(f"ambiguous_resource:{name}")
    return (candidates[0], "reused") if candidates else (await guild.create_text_channel(name, category=category, reason="Portaria Coddy"), "created")


async def _set_view(channel: Any, subject: Any, allowed: bool) -> bool:
    overwrite = channel.overwrites_for(subject)
    if overwrite.view_channel is allowed:
        return False
    overwrite.view_channel = allowed
    await channel.set_permissions(subject, overwrite=overwrite, reason="Reconciliação Coddy")
    return True


async def _apply_portaria_template(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    staff_ids = {str(value) for value in request.get("staffAccessRoleIds", [])}
    roles = {str(role.id): role for role in guild.roles}
    if not staff_ids <= roles.keys():
        raise ValueError("invalid_portaria_staff_roles")
    visitor, visitor_state = await _ensure_role(guild, _clean_name(request.get("visitorRoleName"), "Visitante"))
    from core.auto_join_roles import ensure_auto_join_role_enabled
    ensure_auto_join_role_enabled(guild, visitor.id)
    category, category_state = await _ensure_category(guild, _clean_name(request.get("categoryName"), "Portaria"))
    names = {"welcome": "boas-vindas", "target": "fichas"}
    if request.get("createApproved", True): names["approved"] = "aprovados"
    if request.get("createRejected", True): names["rejected"] = "reprovados"
    channels: dict[str, Any] = {}
    result = {"created": [], "reused": [], "updated": [], "warnings": []}
    for key, name in names.items():
        channel, state = await _ensure_text_channel(guild, category, name)
        channels[key] = channel
        result[state].append({"key": key, "id": str(channel.id)})
    result[visitor_state].append({"key": "visitorRole", "id": str(visitor.id)})
    result[category_state].append({"key": "category", "id": str(category.id)})
    for key, channel in channels.items():
        changed = await _set_view(channel, guild.default_role, False)
        changed |= await _set_view(channel, visitor, key == "welcome")
        for role_id in staff_ids:
            changed |= await _set_view(channel, roles[role_id], True)
        if changed: result["updated"].append({"key": key, "id": str(channel.id)})
    result["resources"] = {"visitanteRoleId": str(visitor.id), "welcomeChannelId": str(channels["welcome"].id), "targetChannelId": str(channels["target"].id),
                           "approvedTargetChannelId": str(channels["approved"].id) if "approved" in channels else None,
                           "rejectedTargetChannelId": str(channels["rejected"].id) if "rejected" in channels else None}
    return result


async def _apply_private_area(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    selected = {str(value) for value in request.get("allowedRoleIds", [])}
    roles = {str(role.id): role for role in guild.roles}
    if not selected or not selected <= roles.keys(): raise ValueError("invalid_private_area_roles")
    category_name = _clean_name(request.get("categoryName"), "Área privada")
    channel_names = _clean_names(request.get("channelNames"), ["privado"])
    _single(list(guild.categories), category_name)
    category, state = await _ensure_category(guild, category_name)
    result = {"created": [], "reused": [], "updated": [], "warnings": []}; result[state].append({"key": "category", "id": str(category.id)})
    for name in channel_names:
        channel, channel_state = await _ensure_text_channel(guild, category, name)
        result[channel_state].append({"key": "channel", "id": str(channel.id)})
        if await _set_view(channel, guild.default_role, False): result["updated"].append({"id": str(channel.id)})
        for role_id in selected:
            if await _set_view(channel, roles[role_id], True): result["updated"].append({"id": str(channel.id), "roleId": role_id})
    return result


async def _apply_visitor_isolation(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    visitor_id = str(request.get("visitorRoleId") or "")
    visitor = next((role for role in guild.roles if str(role.id) == visitor_id), None)
    if visitor is None: raise ValueError("visitor_role_not_found")
    # Kept lazy because importing core.database has legacy pool/DDL side effects.
    entries = await _valid_portaria_entry_channels(guild)
    if not entries: raise ValueError("portaria_entry_channel_not_configured")
    changed = unchanged = 0
    for item in _isolation_targets(guild):
        channel = guild.get_channel(int(item["id"]))
        if channel is None: continue
        if await _set_view(channel, visitor, item["id"] in entries): changed += 1
        else: unchanged += 1
    return {"changedCount": changed, "unchangedCount": unchanged, "skippedCount": 0, "warnings": []}


async def _apply_portaria_permissions(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    # Explicit repair only: it never creates a role or channel.
    visitor_id = str(request.get("visitorRoleId") or "")
    visitor = next((role for role in guild.roles if str(role.id) == visitor_id), None)
    if visitor is None: raise ValueError("visitor_role_not_found")
    staff_ids = {str(value) for value in request.get("staffAccessRoleIds", [])}
    roles = {str(role.id): role for role in guild.roles}
    if str(guild.default_role.id) in staff_ids or not staff_ids <= roles.keys():
        raise ValueError("invalid_portaria_staff_roles")
    channels = [guild.get_channel(int(value)) for value in request.get("channelIds", [])]
    if any(channel is None for channel in channels): raise ValueError("portaria_channel_not_found")
    entries = await _valid_portaria_entry_channels(guild)
    changed = 0
    for channel in channels:
        changed += bool(await _set_view(channel, guild.default_role, False))
        changed += bool(await _set_view(channel, visitor, str(channel.id) in entries))
        for role_id in staff_ids:
            changed += bool(await _set_view(channel, roles[role_id], True))
    return {"changedCount": changed, "unchangedCount": len(channels) * 2 - changed, "skippedCount": 0, "warnings": []}


async def _valid_portaria_entry_channels(guild: discord.Guild) -> set[str]:
    """Fail closed: a publication row alone never grants visitor visibility."""
    from core.database import list_portaria_published_channels
    entries: set[str] = set()
    for publication in list_portaria_published_channels(guild.id):
        channel = guild.get_channel(int(publication["channel_id"]))
        if channel is None or not callable(getattr(channel, "fetch_message", None)):
            continue
        try:
            await channel.fetch_message(int(publication["message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue
        entries.add(str(channel.id))
    return entries


async def _apply_portaria_repair_roles(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    """Repair only the functional Visitante role; never reuse unsafe roles."""
    current_id = str(request.get("visitorRoleId") or "")
    current = next((role for role in guild.roles if str(role.id) == current_id), None)
    if current is not None and current != guild.default_role and not getattr(current, "managed", False):
        from core.auto_join_roles import ensure_auto_join_role_enabled
        ensure_auto_join_role_enabled(guild, current.id)
        return {"created": [], "reused": [], "updated": [], "warnings": [],
                "resources": {"visitanteRoleId": str(current.id)}, "unchangedCount": 1}
    role, state = await _ensure_role(guild, _clean_name(request.get("visitorRoleName"), "Visitante"))
    from core.auto_join_roles import ensure_auto_join_role_enabled
    ensure_auto_join_role_enabled(guild, role.id)
    return {"created": [{"key": "visitorRole", "id": str(role.id)}] if state == "created" else [],
            "reused": [{"key": "visitorRole", "id": str(role.id)}] if state == "reused" else [],
            "updated": [], "warnings": [], "resources": {"visitanteRoleId": str(role.id)}}


async def _apply_portaria_repair_structure(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    """Reconcile configured destinations only. Null optional destinations stay absent."""
    category, category_state = await _ensure_category(guild, _clean_name(request.get("categoryName"), "Portaria"))
    result: dict[str, Any] = {"created": [], "reused": [], "updated": [], "warnings": []}
    result[category_state].append({"key": "category", "id": str(category.id)})
    resources: dict[str, str | None] = {}
    definitions = (
        ("target", "targetChannelId", "fichas", True),
        ("approved", "approvedTargetChannelId", "aprovados", False),
        ("rejected", "rejectedTargetChannelId", "reprovados", False),
    )
    for key, request_key, canonical_name, required in definitions:
        configured_id = request.get(request_key)
        channel = guild.get_channel(int(configured_id)) if str(configured_id or "").isdigit() else None
        if channel is not None:
            resources[request_key] = str(channel.id)
            result["reused"].append({"key": key, "id": str(channel.id)})
            continue
        if not required and configured_id is None:
            resources[request_key] = None
            continue
        channel, state = await _ensure_text_channel(guild, category, canonical_name)
        resources[request_key] = str(channel.id)
        result[state].append({"key": key, "id": str(channel.id)})
    result["resources"] = resources
    return result


async def _publish_portaria_form(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    """Use the same persistent button and publication table as /formulario publicar."""
    flow_id = request.get("flowId")
    channel_id = request.get("welcomeChannelId")
    if not isinstance(flow_id, int) or not str(channel_id or "").isdigit():
        raise ValueError("invalid_portaria_publication")
    has_message = "message" in request
    has_button_text = "buttonText" in request
    if has_message and (not isinstance(request["message"], str) or len(request["message"]) > 2000):
        raise ValueError("invalid_portaria_publication")
    if has_button_text:
        _portaria_button_label(request["buttonText"])
    channel = guild.get_channel(int(channel_id))
    if channel is None or not hasattr(channel, "send"):
        raise ValueError("portaria_entry_channel_not_found")
    from core.database import create_form_published_message, get_form_flow, list_portaria_published_channels
    from core.form_views import FormFlowButtonView
    key = (guild.id, flow_id, channel.id)
    lock = _portaria_publication_locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Re-read persisted state only after the keyed lock is held so concurrent
        # callers cannot both decide that this publication is absent.
        flow = get_form_flow(guild.id, flow_id)
        if not flow or str(flow.get("type", "")).casefold() != "portaria":
            raise ValueError("portaria_flow_not_found")
        for published in list_portaria_published_channels(guild.id):
            if int(published.get("flow_id", 0)) != flow_id or str(published.get("channel_id")) != str(channel.id):
                continue
            try:
                message = await channel.fetch_message(int(published["message_id"]))
            except discord.NotFound:
                continue
            if not has_message and not has_button_text:
                return {"created": [], "reused": [{"key": "publication", "id": str(message.id)}], "updated": [], "warnings": [],
                        "resources": {"messageId": str(message.id), "welcomeChannelId": str(channel.id)}}
            content = _portaria_publication_message(flow, request) if has_message else None
            label = (_portaria_button_label(request["buttonText"]) if has_button_text
                     else _published_portaria_button_label(message, flow_id) or _PORTARIA_DEFAULT_BUTTON_LABEL)
            view = FormFlowButtonView(flow_id, label=label)
            if has_message:
                await message.edit(content=content or None, view=view)
            else:
                await message.edit(view=view)
            bot = getattr(guild, "_state", None) and getattr(guild._state, "_get_client", lambda: None)()
            if bot is not None:
                bot.add_view(view, message_id=message.id)
            return {"created": [], "reused": [], "updated": [{"key": "publication", "id": str(message.id)}], "warnings": [],
                    "resources": {"messageId": str(message.id), "welcomeChannelId": str(channel.id)}}
        content = _portaria_publication_message(flow, request)
        label = _portaria_button_label(request["buttonText"]) if has_button_text else _PORTARIA_DEFAULT_BUTTON_LABEL
        view = FormFlowButtonView(flow_id, label=label)
        message = await channel.send(content=content or None, view=view)
        try:
            create_form_published_message(flow_id, message.id, channel.id, guild.id)
        except Exception:
            try:
                await message.delete()
            except Exception:
                pass
            raise
        bot = getattr(guild, "_state", None) and getattr(guild._state, "_get_client", lambda: None)()
        if bot is not None:
            bot.add_view(view, message_id=message.id)
        return {"created": [{"key": "publication", "id": str(message.id)}], "reused": [], "updated": [], "warnings": [],
                "resources": {"messageId": str(message.id), "welcomeChannelId": str(channel.id)}}


async def _validate_portaria_bypass_target(
    guild: discord.Guild,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Validate a bypass target against the live guild without persisting rules."""

    bypass_type = str(request.get("bypassType") or "").strip().casefold()
    value = request.get("value")

    if bypass_type == "account":
        try:
            discord_user_id = int(str(value))
        except (TypeError, ValueError):
            raise ValueError("invalid_portaria_bypass_account")
        if discord_user_id <= 0:
            raise ValueError("invalid_portaria_bypass_account")

        member = guild.get_member(discord_user_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_user_id)
            except discord.NotFound:
                member = None
        if member is None:
            raise ValueError("portaria_bypass_account_not_in_guild")

        return {
            "bypassType": "account",
            "value": str(member.id),
            "displayName": member.display_name,
        }

    if bypass_type == "invite":
        from core.database import normalize_discord_invite_code

        code = normalize_discord_invite_code(str(value) if value is not None else None)
        if not code:
            raise ValueError("invalid_portaria_bypass_invite")

        # Permission/rate-limit/Discord failures must stay retryable. Only a
        # completed invite listing can prove that a code does not belong here.
        invites = await guild.invites()

        invite = next(
            (
                candidate
                for candidate in invites
                if normalize_discord_invite_code(getattr(candidate, "code", None)) == code
            ),
            None,
        )
        if invite is None:
            raise ValueError("portaria_bypass_invite_not_in_guild")

        return {
            "bypassType": "invite",
            "value": code,
            "displayName": code,
        }

    raise ValueError("invalid_portaria_bypass_type")


_PORTARIA_ROLE_PURPOSES = {"visitor": "Visitante", "provisional": "Carteirinha Provisória", "minor": "18-", "adult": "18+"}
_PORTARIA_CHANNEL_PURPOSES = {"entry": "boas-vindas", "target": "fichas", "approved": "aprovados", "rejected": "reprovados"}


async def _create_portaria_role(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    purpose = request.get("rolePurpose")
    if purpose not in _PORTARIA_ROLE_PURPOSES:
        raise ValueError("invalid_portaria_role_purpose")
    role, state = await _ensure_role(guild, _PORTARIA_ROLE_PURPOSES[purpose])
    if purpose == "visitor":
        from core.auto_join_roles import ensure_auto_join_role_enabled
        ensure_auto_join_role_enabled(guild, role.id)
    return {"created": [{"key": purpose, "id": str(role.id)}] if state == "created" else [],
            "reused": [{"key": purpose, "id": str(role.id)}] if state == "reused" else [], "updated": [],
            "warnings": [], "resources": {"roleId": str(role.id), "rolePurpose": purpose}}


async def _create_portaria_channel(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    purpose = request.get("channelPurpose")
    if purpose not in _PORTARIA_CHANNEL_PURPOSES:
        raise ValueError("invalid_portaria_channel_purpose")
    category = await _portaria_category_for_channel(guild, request)
    channel, state = await _ensure_text_channel(guild, category, _PORTARIA_CHANNEL_PURPOSES[purpose])
    return {"created": [{"key": purpose, "id": str(channel.id)}] if state == "created" else [],
            "reused": [{"key": purpose, "id": str(channel.id)}] if state == "reused" else [], "updated": [], "warnings": [],
            "resources": {"channelId": str(channel.id), "channelPurpose": purpose, "categoryId": str(category.id)}}


async def _portaria_category_for_channel(guild: discord.Guild, request: dict[str, Any]) -> Any:
    category_id = request.get("categoryId")
    if str(category_id or "").isdigit():
        category = guild.get_channel(int(category_id))
        if category is not None and category in guild.categories:
            return category
    category, _ = await _ensure_category(guild, "Portaria")
    return category


async def _delete_portaria_publications(guild: discord.Guild, request: dict[str, Any]) -> dict[str, Any]:
    flow_id = request.get("flowId")
    if not isinstance(flow_id, int):
        raise ValueError("invalid_portaria_flow")
    from core.database import list_portaria_published_channels
    removed = stale = 0
    for publication in list_portaria_published_channels(guild.id):
        if int(publication.get("flow_id", 0)) != flow_id:
            continue
        channel = guild.get_channel(int(publication["channel_id"]))
        if channel is None:
            stale += 1
            continue
        try:
            message = await channel.fetch_message(int(publication["message_id"]))
            await message.delete(reason="Formulário Portaria removido pelo painel")
            removed += 1
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            stale += 1
    return {"created": [], "reused": [], "updated": [], "removed": [{"key": "publication"}] * removed,
            "warnings": [f"{stale} publicação(ões) stale ignorada(s)."] if stale else []}
