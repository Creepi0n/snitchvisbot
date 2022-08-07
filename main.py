from datetime import datetime, timedelta
from tempfile import NamedTemporaryFile, TemporaryDirectory
import sqlite3
from pathlib import Path
from asyncio import Queue
from concurrent.futures import ProcessPoolExecutor
from functools import partial
import gzip

from discord import File
from snitchvis import (Event, InvalidEventException, SnitchVisRecord,
    create_users, snitches_from_events, Snitch)
from PyQt6.QtWidgets import QApplication

import db
import utils
from secret import TOKEN
from command import command, Arg, channel, role, human_timedelta, human_datetime
from client import Client

INVITE_URL = ("https://discord.com/oauth2/authorize?client_id="
    "999808708131426434&permissions=0&scope=bot")
LOG_CHANNEL = 1002607241586823270
DEFAULT_PREFIX = "."

def run_snitch_vis(snitches, events, users, size, fps, duration, all_snitches,
    fade, event_mode, output_file
):
    vis = SnitchVisRecord(snitches, events, users, size, fps,
        duration, all_snitches, fade, event_mode, output_file)
    vis.render()

class Snitchvis(Client):
    # for reference, a 5 second video of 700 pixels at 30 fps is 70 million
    # pixels. A 60 second video of 1000 pixels at 30 fps is 1.8 billion pixels.
    # PIXEL_LIMIT_VIDEO =   2_000_000_000
    PIXEL_LIMIT_VIDEO =   2_000_000_00000000000
    # 100 billion pixels is roughly 13 minutes of 1080p @ 60fps
    # (1920*1080*60*60*13 = 97_044_480_000). This is excessively high, and
    # nobody who is not trying to abuse the bot will hit this limit.
    # PIXEL_LIMIT_DAY =   100_000_000_000
    PIXEL_LIMIT_DAY =   100_000_000_00000000000000

    def __init__(self, *args, **kwargs):
        super().__init__(DEFAULT_PREFIX, LOG_CHANNEL, *args, **kwargs)
        # there's a potential race condition when indexing messages on startup,
        # where we spend x seconds indexing channels before some channel c,
        # but than at y < x seconds a new message comes in to channel c which
        # gets processed and causes the last indexed id to be set to a very high
        # id, causing us not to index the messages we didn't see while we were
        # down when self.index_channel gets called on c.
        # To prevent this, we'll stop indexing new messages at all while
        # indexing channels on startup, and instead stick the new messages into
        # a queue. This queue will be processed in the order the messages were
        # received once we're done indexing the channels and can be assured we
        # won't mess up our last_indexed_id.
        self.defer_indexing = False
        self.indexing_queue = Queue()

    async def on_ready(self):
        await super().on_ready()
        print("connected to discord")
        # avoid last_indexed_id getting set to a wrong value by incoming
        # messages while we index channels
        self.defer_indexing = True
        # index any messages sent while we were down
        for channel in db.get_snitch_channels(None):
            c = self.get_channel(channel.id)
            permissions = c.permissions_for(c.guild.me)
            if not permissions.read_messages:
                print(f"Couldn't index {c} / {c.id} (guild {c.guild} / "
                    f"{c.guild.id}) without read_messages permission")
                continue
            await self.index_channel(channel, c)
        db.commit()

        # index messages in the order we received them now that it's safe to do
        # so. New messages might get added to the queue while we're in the
        # middle of processing these, so it's important to continuously poll the
        # queue.
        while not self.indexing_queue.empty():
            message = await self.indexing_queue.get()
            await self.maybe_index_message(message)

        # now that we've indexed the channels and fully processed the queue, we
        # can go back to indexing new messages normally.
        self.defer_indexing = False

    async def on_message(self, message):
        await super().on_message(message)
        if not self.defer_indexing:
            await self.maybe_index_message(message)
        else:
            self.indexing_queue.put_nowait(message)

    async def maybe_index_message(self, message):
        snitch_channel = db.get_snitch_channel(message.channel)
        # only index messages in snitch channels which have been fully indexed
        # by `.index` already. If someone adds a snitch channel with
        # `.channel add #snitches`, and then a snitch ping is immediately sent
        # in that channel, we don't want to update the last indexed id (or
        # index the message at all) until the channel has been fully indexed
        # manually.
        if not snitch_channel or not snitch_channel.last_indexed_id:
            return

        try:
            event = Event.parse(message.content)
        except InvalidEventException:
            return

        db.add_event(message, event)
        db.update_last_indexed(message.channel, message.id)

    async def index_channel(self, channel, discord_channel):
        print(f"Indexing channel {discord_channel} / {discord_channel.id}, "
            f"guild {discord_channel.guild} / {discord_channel.guild.id}")
        events = []
        last_id = channel.last_indexed_id
        async for message_ in discord_channel.history(limit=None):
            # don't index past the last indexed message id (if we have such
            # an id stored)
            if last_id and message_.id <= last_id:
                break

            try:
                event = Event.parse(message_.content)
            except InvalidEventException:
                continue
            events.append([message_, event])

        last_messages = await discord_channel.history(limit=1).flatten()
        # only update if the channel has messages
        if last_messages:
            last_message = last_messages[0]
            db.update_last_indexed(channel, last_message.id, commit=False)

        for (message_, event) in events:
            # caller is responsible for committing
            db.add_event(message_, event, commit=False)

        return events

    async def export_to_sql(self, path, snitches, events):
        conn = sqlite3.connect(str(path))
        c = conn.cursor()

        c.execute(
            """
            CREATE TABLE event (
                `username` TEXT NOT NULL,
                `snitch_name` TEXT,
                `namelayer_group` TEXT NOT NULL,
                `x` INTEGER NOT NULL,
                `y` INTEGER NOT NULL,
                `z` INTEGER NOT NULL,
                `t` INTEGER NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE snitch (
                world TEXT,
                x INT,
                y INT,
                z INT,
                group_name TEXT,
                type TEXT,
                name TEXT,
                dormant_ts BIGINT,
                cull_ts BIGINT,
                first_seen_ts BIGINT,
                last_seen_ts BIGINT,
                created_ts BIGINT,
                created_by_uuid TEXT,
                renamed_ts BIGINT,
                renamed_by_uuid TEXT,
                lost_jalist_access_ts BIGINT,
                broken_ts BIGINT,
                gone_ts BIGINT,
                tags TEXT,
                notes TEXT
            )
            """
        )
        c.execute("""
            CREATE UNIQUE INDEX snitch_world_x_y_z_unique
            ON snitch(world, x, y, z);
        """)
        conn.commit()

        for snitch in snitches:
            args = [
                snitch.world, snitch.x, snitch.y, snitch.z,
                snitch.group_name, snitch.type, snitch.name,
                snitch.dormant_ts, snitch.cull_ts, snitch.first_seen_ts,
                snitch.last_seen_ts, snitch.created_ts,
                snitch.created_by_uuid, snitch.renamed_ts,
                snitch.renamed_by_uuid, snitch.lost_jalist_access_ts,
                snitch.broken_ts, snitch.gone_ts, snitch.tags, snitch.notes
            ]
            # ignore duplicate snitches
            c.execute("INSERT OR IGNORE INTO snitch VALUES (?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", args)

        for event in events:
            args = [
                event.username, event.snitch_name, event.namelayer_group,
                event.x, event.y, event.z, event.t.timestamp()
            ]
            c.execute("INSERT INTO event VALUES (?, ?, ?, ?, ?, ?, ?)", args)
        conn.commit()


    @command("tutorial",
        help="Walks you through an initial setup of snitchvis."
    )
    async def tutorial(self, message):
        await message.channel.send("To set up snitchvis, you'll need to do two "
            "things:\n* add snitch channels, so snitchvis knows where to look "
            "for snitch events (pings/logins/logouts)"
            "\n* index snitch channels so snitchvis actually has the events "
            "stored. This is a separate command because it can take a long "
            "time to retrieve all the snitch messages from discord due to "
            "ratelimiting.")
        await message.channel.send("To add a snitch channel, use the "
            "`.channel add` command (see also `.channel add --help`). This "
            "should be the same channel you've previously set up a kira relay "
            "for. Adding a "
            "snitch channel requires that you specify which discord roles "
            "should be able to render events from this snitch channel - this "
            "usually will be the same roles you've given permission to view "
            "the channel on discord, but it doesn't have to be.\n"
            "If you mess up the roles when adding a "
            "snitch channel, you can use `.channel remove` to remove it, then "
            "re-add it with the correct roles.")
        await message.channel.send("Once you've added all your snitch channels "
            "with the desired role access, it's time to tell snitchvis to "
            "index all the events in those channels. Run `.index` to do so. "
            "Running this command is only necessary whenever you add a new "
            "snitch channel - snitchvis will automatically index new messages "
            "in snitch channels after this command has been run.")
        await message.channel.send("Once `.index` finishes, you're ready to "
            "render some snitches! The command to render is `.render` or `.r`. "
            "When run with no arguments, it looks for the most recent event "
            "in any snitch channel, then renders the past 30 minutes of events "
            "before that event. This is meant to be a quick way to take a look "
            "at the most recent snitch pings on your network.")
        await message.channel.send("`.r` supports a wide variety of different "
            "options, however, and you should take some time to read through "
            "them and try them out - this is the most powerful part (and "
            "primary feature) of snitchvis. For instance, to render all events "
            "from the past day and a half, run `.r --past 1d12h` (or "
            "equivalently `.r -p 1d12h`). You can also filter events by users, "
            "make the video longer, higher or lower quality, and more. Run "
            "`.r --help` to see all available options. Feel free to play "
            "around with it!")


    @command("channel add",
        args=[
            Arg("channels", nargs="+", convert=channel, help="The "
                "channels to add. Use a proper channel mention "
                "(eg #snitches) to specify a channel."),
            Arg("-r", "--roles", nargs="+", convert=role, help="The roles "
                "which will be able to render events from this channel. Use the "
                "name of the role (don't ping the role). Use the name "
                "`everyone` to grant all users access to render the snitches.")
        ],
        help="Adds a snitch channel(es), viewable by the specified roles.",
        permissions=["manage_guild"]
    )
    async def channel_add(self, message, channels, roles):
        for channel in channels:
            if db.snitch_channel_exists(channel):
                await message.channel.send(f"{channel.mention} is already a "
                    "snitch channel. If you would like to change which roles "
                    f"have access to {channel.mention}, first remove it "
                    "(`.channel remove`) then re-add it (`.channel add`) with "
                    "the desired roles.")
                return
            db.add_snitch_channel(channel, roles)

        await message.channel.send(f"Added {utils.channel_str(channels)} to "
            f"snitch channels.")


    @command("channel remove",
        args=[
            Arg("channels", nargs="+", convert=channel, help="The "
                "channels to remove. Use a proper channel mention "
                "(eg #snitches) to specify a channel.")
        ],
        help="Removes a snitch channel(es) from the list of snitch channels.",
        permissions=["manage_guild"]
    )
    async def channel_remove(self, message, channels):
        for channel in channels:
            db.remove_snitch_channel(channel)

        await message.channel.send(f"Removed {utils.channel_str(channels)} "
            "from snitch channels.")


    @command("channel list",
        help="Lists the current snitch channels and what roles can view them.",
        permissions=["manage_guild"]
    )
    async def channel_list(self, message):
        channels = db.get_snitch_channels(message.guild)
        if not channels:
            await message.channel.send("No snitch channels set. You can add "
                "snitch channels with `.channel add`.")
            return

        m = "Current snitch channels:\n"
        for channel in channels:
            m += f"\n{utils.channel_accessible(message.guild, channel)}"
        await message.channel.send(m)


    @command("index",
        help="Indexes messages in the current snitch channels.",
        permissions=["manage_guild"]
    )
    async def index(self, message):
        channels = db.get_snitch_channels(message.guild)

        if not channels:
            await message.channel.send("No snitch channels to index. Use "
                "`.channel add #channel` to add snitch channels.")
            return

        await message.channel.send("Indexing the following snitch channels: "
            f"{utils.channel_str(channels)}. This could take a LONG time if "
            "they have lots of messages in them.")

        for channel in channels:
            # make sure we can read all the snitch channels
            c = channel.to_discord(message.guild)
            permissions = c.permissions_for(message.guild.me)
            if not permissions.read_messages:
                await message.channel.send("Snitchvis doesn't have permission "
                    f"to read messages in {channel.mention}. Either give "
                    "snitchvis enough permissions to read messages there, or "
                    "remove it from the list of snitch channels (with "
                    "`.channel remove`).")
                return

        for channel in channels:
            await message.channel.send(f"Indexing {channel.mention}...")
            c = channel.to_discord(message.guild)
            events = await self.index_channel(channel, c)
            db.commit()

            await message.channel.send(f"Added {len(events)} new events from "
                f"{channel.mention}")

        await message.channel.send("Finished indexing snitch channels")

    @command("full-reindex",
        args=[
            Arg("-y", store_boolean=True, help="Pass to confirm you would like "
            "to reindex the server.")
        ],
        help="Drops all currently indexed snitches and re-indexes from "
            "scratch. This can help with some rare issues. You probably don't "
            "want to do this unless you know what you're doing, or have been "
            "advised to do so by tybug.",
        help_short="Drops all currently indexed snitches and re-indexes from "
            "scratch.",
        permissions=["manage_guild"]
    )
    async def full_reindex(self, message, y):
        if not y:
            await message.channel.send("This command will delete all currently "
                "indexed snitches and will re-index from scratch. This can "
                "help with some rare issues. You probably don't want to do "
                "this unless you know what you're doing, or have been advised "
                "to do so by tybug. If you're sure you would like to reindex, "
                "run `.full-reindex -y`.")
            return
        await message.channel.send("Dropping all events and resetting last "
            "indexed ids")
        # drop all events
        db.execute("DELETE FROM event WHERE guild_id = ?", [message.guild.id])
        # reset last indexed id so indexing works from scratch again
        db.execute("UPDATE snitch_channel SET last_indexed_id = null "
            "WHERE guild_id = ?", [message.guild.id])
        # finally, reindex.
        await self.index(message)

    @command("render",
        # TODO make defaults for these parameters configurable
        args=[
            Arg("-a", "--all-snitches", default=False, store_boolean=True,
                help="If passed, all known snitches will be rendered, not "
                "just the snitches pinged by the relevant events. Warning: "
                "this can result in very small or unreadable event fields."),
            Arg("-s", "--size", default=700, convert=int, help="The resolution "
                "of the render, in pixels. Defaults to 700. Decrease if "
                "you want faster renders, increase if you want higher quality "
                "renders."),
            Arg("-f", "--fps", default=20, convert=int, help="The frames per "
                "second of the render. Defaults to 20. Decrease if you want "
                "faster renders, increase if you want smoother renders."),
            Arg("-d", "--duration", default=5, convert=int, help="The duration "
                "of the render, in seconds. Defaults to 5 seconds. If you want "
                "to take a slower, more "
                "careful look at events, specify a higher value. If you just "
                "want a quick glance, specify a lower value. Higher values "
                "take longer to render."),
            Arg("-u", "--users", nargs="*", default=[], help="If passed, only "
                "events by these users will be rendered."),
            Arg("-p", "--past", convert=human_timedelta, help="How far in the "
                "past to look for events. Specify in human-readable form, ie "
                "-p 1y2mo5w2d3h5m2s (\"1 year 2 months 5 weeks 2 days 3 hours 5 "
                "minutes 2 seconds ago\"), or any combination thereof, ie "
                "-p 1h30m (\"1 hour 30 minutes ago\"). Use the special value "
                "\"all\" to render all events."),
            Arg("--start", convert=human_datetime, help="The start date of "
                "events to include. Use the format `mm/dd/yyyy` or `mm/dd/yy`, "
                "eg 7/18/2022 or 12/31/21. If --start is passed but not "
                "--end, *all* events after the passed start date will be "
                "rendered."),
            Arg("--end", convert=human_datetime, help="The end date of "
                "events to include. Use the format `mm/dd/yyyy` or `mm/dd/yy`, "
                "eg 7/18/2022 or 12/31/21. If --end is passed but not "
                "--start, *all* events before the passed end date will be "
                "rendered."),
            Arg("--fade", default=10, convert=float, help="What percentage of "
                "the video duration event highlighting will be visible for. At "
                "--fade 100, every event will remain on screen for the entire "
                "render. At --fade 50, events will remain on screen for half "
                "the render. Fade duration is limited to a minimum of 1.5 "
                "seconds regardless of what you specify for --fade. Defaults "
                "to 10% of video duration (equivalent to --fade 10)."),
            Arg("-l", "--line", store_boolean=True, help="Draw lines "
                "between snitch events instead of the default boxes around "
                "individual snitch events. This option is "
                "experimental and may not look good. It is intended to "
                "provide an easier way to see directionality and travel "
                "patterns than the default mode, and may eventually become the "
                "default mode."),
            Arg("-g", "--groups", nargs="*", default=[], help="If passed, only "
                "events from snitches on these namelayer groups will be "
                "rendered."),
            # TODO work on svis file format
            Arg("--export", help="Export the events matching the specified "
                "criteria to either an sql database, or an .svis file (for use "
                "in the Snitch Vis desktop application). Pass `--export sql` "
                "the former and `--export svis` for the latter.")
        ],
        help="Renders snitch events to a vidoe. Provides options to adjust "
            "render look and feel, events included, duration, quality, etc.",
        aliases=["r"]
    )
    async def render(self, message, all_snitches, size, fps, duration, users,
        past, start, end, fade, line, groups, export
    ):
        NO_EVENTS = ("No events match those criteria. Try adding snitch "
            "channels with `.channel add #channel`, indexing with `.index`, or "
            "adjusting your parameters to include more snitch events.")

        # TODO do this validation in argparse
        if export and export not in ["sql", "svis"]:
            await message.channel.send("`--export` must be one of `sql`, "
                "`svis`")
            return

        if past:
            end = datetime.utcnow().timestamp()
            if past == "all":
                # conveniently, start of epoch is 0 ms
                start = 0
            else:
                start = end - past.total_seconds()
        else:
            if not start and not end:
                # neither set
                # slightly special behavior: instead of going back in the past
                # `x` ms, go back to the most recent event (however long ago
                # that may be) and *then* go back `x` ms and grab all those
                # events.
                event = db.most_recent_event(message.guild)
                # if the guild doesn't have any events at all yet, complain and
                # exit.
                if not event:
                    await message.channel.send(NO_EVENTS)
                    return
                end = event.t.timestamp()
                # TODO make adjustable instead of hardcoding 30 minutes, not
                # sure what parameter name to use though (--past-adjusted?)
                start = end - (30 * 60)
            elif start and not end:
                # only start set. Set end to current date
                start = start.timestamp()
                end = datetime.utcnow().timestamp()
            elif end and not start:
                # only end set. Set start to beginning of time
                start = 0
                end = end.timestamp()
            else:
                # both set
                start = start.timestamp()
                end = end.timestamp()

        if end < start:
            await message.channel.send("End date can't be before start date.")
            return

        # TODO warn if no events by the specified users are in the events filter
        events = db.get_events(message.guild, message.author, start, end, users,
            groups)

        if not events:
            await message.channel.send(NO_EVENTS)
            return

        all_events = db.get_all_events(message.guild)
        # use all known events to construct snitches
        snitches = snitches_from_events(all_events)
        # if the guild has any snitches uploaded (via .import-snitches), use
        # those as well, even if they've never been pinged.
        # Only retrieve snitches which the author has access to via their roles
        snitches |= set(db.get_snitches(message.guild, message.author.roles))
        users = create_users(events)

        if export == "sql":
            await message.channel.send("Exporting specified events to a "
                "database...")
            with TemporaryDirectory() as d:
                d = Path(d)
                p = d / "snitchvis_export.sqlite"
                zipped_p = d / "snitchvis_export.sqlite.gz"
                await self.export_to_sql(p, snitches, events)

                # compress with gzip
                with open(p, "rb") as f_in:
                    with gzip.open(zipped_p, "wb") as f_out:
                        f_out.writelines(f_in)

                sql_file = File(zipped_p)
                await message.channel.send(file=sql_file)
            return

        num_pixels = duration * fps * (size * size)
        if num_pixels > self.PIXEL_LIMIT_VIDEO:
            await message.channel.send("The requested render would require too "
                "many server resources to generate. Decrease either the render "
                "size (`-s/--size`), fps (`-f/--fps`), or duration "
                "(`-d/--duration`).")
            return

        start = (datetime.now() - timedelta(days=1)).timestamp()
        end = datetime.now().timestamp()
        usage = db.get_pixel_usage(message.guild, start, end)
        if usage > self.PIXEL_LIMIT_DAY:
            await message.channel.send("You've rendered more than 100 billion "
                "pixels in the past 24 hours. I have limited server resources "
                "and cannot allow servers to render more than this (already "
                "extremely high) limit per day. You will have to wait up to "
                "24 hours for your usage to decrease before being able to "
                "render again.")
            return

        with TemporaryDirectory() as d:
            output_file = str(Path(d) / "out.mp4")

            m = await message.channel.send("rendering video...")

            # seconds to ms
            duration *= 1000
            event_mode = "line" if line else "square"

            # if we run this in the default executor (ThreadPoolExecutor), we
            # get a pretty bad memory leak. We spike to ~700mb on a default
            # settings visualization (5 seconds / 20 fps / 700 pixels), which is
            # normal enough, but then
            # instead of returning to the baseline 70mb, we return to 350mb or
            # so after rendering. It's not a true memory leak though because
            # subsequent renders don't always increase memory: if you continue
            # to render at default settings, it'll return to 350mb pretty much
            # every time. If you then render something larger (-s 1000 or so),
            # it'll spike to 1200mb (again, normal) but then return to 500mb or
            # so instead of 350mb. It's like it sticks to a high water mark or
            # something. But it's not just that because memory usage does also
            # go up non insignificant amounts at random intervals when you
            # render.
            # I'm not sure what's leaking - the obvious culprits are the ffmpeg
            # pipe, the images, qbuffers, or the world pixmap. But all of those
            # should be getting cleaned up when `SnitchVisRecord` gets gc'd, so
            # I dunno.
            # This memory leak is something I definitely should look into and
            # fix at some point, but I don't want to right now, so the temporary
            # fix is sticking the visualization into a separate process and
            # letting 100% of its memory get returned to the OS when it exits,
            # since its only job is writing to an output mp4.
            # We are taking a slight hit on the event pickling, but hopefully
            # it's not too bad.
            f = partial(run_snitch_vis, snitches, events, users, size, fps,
                duration, all_snitches, fade, event_mode, output_file)
            with ProcessPoolExecutor() as pool:
                await self.loop.run_in_executor(pool, f)

            vis_file = File(output_file)
            await message.channel.send(file=vis_file)
            await m.delete()

            # hardcode some ids (eg me) to not send log mesages for
            if message.author.id not in [216008405758771200]:
                vis_file = File(output_file)
                await self.log_channel.send(file=vis_file)

        db.add_render_history(message.guild, num_pixels,
            datetime.now().timestamp())

    @command("import-snitches",
        args=[
            Arg("-g", "--groups", nargs="+", help="Only snitches in the "
                "database which are reinforced to one of these groups will be "
                "imported. If you really want to import all snitches in the "
                "database, pass `-g all`."),
            Arg("-r", "--roles", nargs="+", convert=role, help="Users with at "
                "least one of these roles will be able to render the "
                "imported snitches. Use the name of the role (don't ping the "
                "role). Use the name `everyone` to grant all users access to "
                "the snitches.")
        ],
        help="Imports snitches from a SnitchMod database.\n"
            "You will likely have to use this command multiple times on the "
            "same database if you have a tiered hierarchy of snitch groups; "
            "for instance, you might run `.import-snitches -g mta-citizens "
            "mta-shops -r citizen` to import snitches citizens can render, "
            "and then `.import-snitches -g mta-cabinet -r cabinet` to import "
            "snitches only cabinet members can render.",
        help_short="Imports snitches from a SnitchMod database.",
        permissions=["manage_guild"]
    )
    async def import_snitches(self, message, groups, roles):
        attachments = message.attachments
        if not attachments:
            await message.channel.send("You must upload a `snitch.sqlite` file "
                "in the same message as the `.import-snitches` command.")
            return

        with NamedTemporaryFile() as f:
            attachment = attachments[0]
            await attachment.save(f.name)
            conn = sqlite3.connect(f.name)
            cur = conn.cursor()

            for group in groups:
                if group == "all":
                    continue
                row = cur.execute("SELECT COUNT(*) FROM snitches_v2 WHERE "
                    "group_name = ?", [group]).fetchone()
                if row[0] == 0:
                    await message.channel.send("No snitches on namelayer "
                        f"group `{group}` found in this database. If the "
                        "group name is correct, omit it and re-run to "
                        "avoid this error.")
                    await message.channel.send("Import aborted. You may "
                        "safely re-run this import with different "
                        "parameters.")
                    return

            await message.channel.send("Importing snitches from snitchmod "
                "database...")

            snitches_added = 0
            if any(group == "all" for group in groups):
                group_filter = "1"
            else:
                group_filter = f"group_name IN ({('?, ' * len(groups))[:-2]}"

            rows = cur.execute("SELECT * FROM snitches_v2 WHERE "
                f"{group_filter}").fetchall()

            for row in rows:
                snitch = Snitch.from_snitchmod(row)
                # batch commit for speed
                rowcount = db.add_snitch(message.guild, snitch, roles,
                    commit=False)
                snitches_added += rowcount

            db.commit()

        await message.channel.send(f"Added {snitches_added} new snitches.")

    @command("permissions",
        help="Lists what snitch channels you have "
        "have permission to render events from. This is based on your discord "
        "roles and how you set up the snitch channels (see `.channel list`).",
        help_short="Lists what snitch channels you have permission to render "
            "events from."
    )
    async def permissions(self, message):
        # tells the command author what snitch channels they can view.
        snitch_channels = db.get_snitch_channels(message.guild)

        channels = set()
        for role in message.author.roles:
            for channel in snitch_channels:
                if role.id in channel.allowed_roles:
                    channels.add(channel)

        if not channels:
            await message.channel.send("You can't render any events.")
            return

        await message.channel.send("You can render events from the "
            f"following channels: {utils.channel_str(channels)}")

    @command("events", help="Lists the most recent events for the specified "
        "snitch or snitches.",
        args=[
            Arg("-n", "--name", help="List events for snitches with the "
                "specified name."),
            Arg("-l", "--location", help="List events for snitches at this "
                "location. Format is `-l/--location x y z` or "
                "`-l/--location x z`. The two parameter version is a "
                "convenience to avoid having to specify a y level; snitches at "
                "all y levels at that (x, z) location will be searched for "
                "events.", nargs="*")
        ],
        # TODO temporary until fix permissions
        permissions=["manage_guild"]
    )
    async def events(self, message, name, location):
        # explicitly allow empty name, useful for searching for unnamed snitches
        if not (bool(name) or name == "") ^ bool(location):
            await message.channel.send("Exactly one of `-n/--name` or "
                "`-l/--location` must be passed.\nRun `.events --help` for "
                "more information.")
            return

        if name is not None:
            events = db.select("""
                SELECT * FROM event
                WHERE guild_id = ? AND snitch_name = ?
                ORDER BY t DESC LIMIT 10
            """, [message.guild.id, name])
        elif location:
            if len(location) == 2:
                x, z = location
                y = None
            elif len(location) == 3:
                x, y, z = location
            else:
                await message.channel.send(f"Invalid location "
                    f"`{' '.join(location)}`. Must be in the form "
                    "`-l/--location x y z` or `-l/--location x z`")
                return

            try:
                x = int(x)
            except ValueError:
                await message.channel.send(f"Invalid x coordinate `{x}`")
                return

            try:
                y = int(y) if y else None
            except ValueError:
                await message.channel.send(f"Invalid y cooridnate `{y}`")
                return

            try:
                z = int(z)
            except ValueError:
                await message.channel.send(f"Invalid z coordinate `{z}`")
                return

            # swap y and z because that's what the db expects
            if y is not None:
                events = db.select("""
                    SELECT * FROM event
                    WHERE guild_id = ? AND x = ? AND y = ? AND z = ?
                    ORDER BY t DESC LIMIT 10
                """, [message.guild.id, x, z, y])
            else:
                events = db.select("""
                    SELECT * FROM event
                    WHERE guild_id = ? AND x = ? AND y = ?
                    ORDER BY t DESC LIMIT 10
                """, [message.guild.id, x, z])

        if not events:
            await message.channel.send("No events match those criteria.")
            return

        messages = []
        for event in events:
            t = datetime.fromtimestamp(event["t"]).strftime('%Y-%m-%d %H:%M:%S')
            group = event["namelayer_group"]
            username = event["username"]
            snitch_name = event["snitch_name"]
            x = event["x"]
            y = event["y"]
            z = event["z"]

            messages.append(f"`[{t}]` `[{group}]` **{username}** is at "
                f"{snitch_name} ({x},{z},{y})")
        await message.channel.send("10 most recent events matching those "
            "criteria (most recent first):\n" + "\n".join(messages))

    @command("help", help="Displays available commands.")
    async def help(self, message):
        command_texts = []
        for command in self.commands:
            # don't show aliases in help (yet, we probably want a separate
            # section or different display method for them)
            if command.alias:
                continue
            # TODO display custom prefixes if set
            prefix = self.default_prefix if command.use_prefix else ""
            command_texts.append(f"  {prefix}{command.name}: "
                f"{command.help_short}")

        await message.channel.send("```\n" + "\n".join(command_texts) + "```\n")

    @command("snitchvissetprefix",
        help="Sets a new prefix for snitchvis. The default prefix is `.`.",
        args=[
            Arg("prefix", help="The new prefix to use. Must be a single "
            "character.")
        ],
        use_prefix=False,
        permissions=["manage_guild"]
    )
    async def set_prefix(self, message, prefix):
        if len(prefix) != 1:
            await message.channel.send("New prefix must be a single character.")
            return

        db.set_guild_prefix(message.guild, prefix)
        # update cached prefix immediately, this updates on bot restart normally
        self.prefixes[message.guild.id] = prefix

        await message.channel.send(f"Successfully set prefix to `{prefix}`.")

# we can only have one qapp active at a time, but we want to be able to
# be rendering multiple snitch logs at the same time (ie multiple .v
# commands, potentially in different servers). We'll keep a master qapp
# active at the top level, but never exec it, which is enough to let us
# draw on qimages and generate videos with SnitchVisRecord and
# FrameRenderer.
# https://stackoverflow.com/q/13215120 for platform/minimal args
qapp = QApplication(['-platform', 'minimal'])

if __name__ == "__main__":
    client = Snitchvis()
    client.run(TOKEN)

# TODO make lines mode in visualizer actually worth using - highlight single
# events, distinguish actual events and the lines, add arrows to indicate
# directionality

# TODO add "centered at (x, y)" coordinates to info text, can be confusing where
# the vis is sometimes

# TODO support custom kira message formats

# TODO fix permissions on .events, currently returns results for all events,
# need to limit to just the events the user has access to

# TODO need padding for visible snitches, we care about the *snitch field*
# being visible, not the snitch itself being visible
# https://discord.com/channels/993250058801774632/993536931189244045/1002667797907775598

# TODO -c/--context n render option to expand the bounding box by n blocks, for
# when you want to see more context. MIN_BOUNDING_BOX_SIZE helps with this but
# isn't a perfect solution

# TODO add regex filtering to .events --name, seems useful (--name-regex?)
