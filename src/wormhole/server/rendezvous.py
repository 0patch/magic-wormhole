from __future__ import print_function
import os, time, random, base64
from collections import namedtuple
from twisted.python import log
from twisted.application import service, internet

SECONDS = 1.0
MINUTE = 60*SECONDS
HOUR = 60*MINUTE
DAY = 24*HOUR
MB = 1000*1000

CHANNEL_EXPIRATION_TIME = 3*DAY
EXPIRATION_CHECK_PERIOD = 2*HOUR

def get_sides(row):
    return set([s for s in [row["side1"], row["side2"]] if s])
def make_sides(sides):
    return list(sides) + [None] * (2 - len(sides))
def generate_mailbox_id():
    return base64.b32encode(os.urandom(8)).lower().strip(b"=").decode("ascii")


SideResult = namedtuple("SideResult", ["changed", "empty", "side1", "side2"])
Unchanged = SideResult(changed=False, empty=False, side1=None, side2=None)
class CrowdedError(Exception):
    pass

def add_side(row, new_side):
    old_sides = [s for s in [row["side1"], row["side2"]] if s]
    assert old_sides
    if new_side in old_sides:
        return Unchanged
    if len(old_sides) == 2:
        raise CrowdedError("too many sides for this thing")
    return SideResult(changed=True, empty=False,
                      side1=old_sides[0], side2=new_side)

def remove_side(row, side):
    old_sides = [s for s in [row["side1"], row["side2"]] if s]
    if side not in old_sides:
        return Unchanged
    remaining_sides = old_sides[:]
    remaining_sides.remove(side)
    if remaining_sides:
        return SideResult(changed=True, empty=False, side1=remaining_sides[0],
                          side2=None)
    return SideResult(changed=True, empty=True, side1=None, side2=None)

Usage = namedtuple("Usage", ["started", "waiting_time", "total_time", "result"])
TransitUsage = namedtuple("TransitUsage",
                          ["started", "waiting_time", "total_time",
                           "total_bytes", "result"])

SidedMessage = namedtuple("SidedMessage", ["side", "phase", "body",
                                           "server_rx", "msg_id"])

class Mailbox:
    def __init__(self, app, db, blur_usage, log_requests, app_id, mailbox_id):
        self._app = app
        self._db = db
        self._blur_usage = blur_usage
        self._log_requests = log_requests
        self._app_id = app_id
        self._mailbox_id = mailbox_id
        self._listeners = {} # handle -> (send_f, stop_f)
        # "handle" is a hashable object, for deregistration
        # send_f() takes a JSONable object, stop_f() has no args

    def open(self, side, when):
        # requires caller to db.commit()
        assert isinstance(side, type(u"")), type(side)
        db = self._db
        row = db.execute("SELECT * FROM `mailboxes`"
                         " WHERE `app_id`=? AND `id`=?",
                         (self._app_id, self._mailbox_id)).fetchone()
        try:
            sr = add_side(row, side)
        except CrowdedError:
            db.execute("UPDATE `mailboxes` SET `crowded`=?"
                       " WHERE `app_id`=? AND `id`=?",
                       (True, self._app_id, self._mailbox_id))
            db.commit()
            raise
        if sr.changed:
            db.execute("UPDATE `mailboxes` SET"
                       " `side1`=?, `side2`=?, `second`=?"
                       " WHERE `app_id`=? AND `id`=?",
                       (sr.side1, sr.side2, when,
                        self._app_id, self._mailbox_id))

    def get_messages(self):
        messages = []
        db = self._db
        for row in db.execute("SELECT * FROM `messages`"
                              " WHERE `app_id`=? AND `mailbox_id`=?"
                              " ORDER BY `server_rx` ASC",
                              (self._app_id, self._mailbox_id)).fetchall():
            sm = SidedMessage(side=row["side"], phase=row["phase"],
                              body=row["body"], server_rx=row["server_rx"],
                              msg_id=row["msg_id"])
            messages.append(sm)
        return messages

    def add_listener(self, handle, send_f, stop_f):
        self._listeners[handle] = (send_f, stop_f)
        return self.get_messages()

    def remove_listener(self, handle):
        self._listeners.pop(handle)

    def broadcast_message(self, sm):
        for (send_f, stop_f) in self._listeners.values():
            send_f(sm)

    def _add_message(self, sm):
        self._db.execute("INSERT INTO `messages`"
                         " (`app_id`, `mailbox_id`, `side`, `phase`,  `body`,"
                         "  `server_rx`, `msg_id`)"
                         " VALUES (?,?,?,?,?, ?,?)",
                         (self._app_id, self._mailbox_id, sm.side,
                          sm.phase, sm.body, sm.server_rx, sm.msg_id))
        self._db.commit()

    def add_message(self, sm):
        assert isinstance(sm, SidedMessage)
        self._add_message(sm)
        self.broadcast_message(sm)

    def close(self, side, mood, when):
        assert isinstance(side, type(u"")), type(side)
        db = self._db
        row = db.execute("SELECT * FROM `mailboxes`"
                         " WHERE `app_id`=? AND `id`=?",
                         (self._app_id, self._mailbox_id)).fetchone()
        if not row:
            return
        sr = remove_side(row, side)
        if sr.empty:
            rows = db.execute("SELECT DISTINCT(`side`) FROM `messages`"
                              " WHERE `app_id`=? AND `mailbox_id`=?",
                              (self._app_id, self._mailbox_id)).fetchall()
            num_sides = len(rows)
            self._summarize_and_store(row, num_sides, mood, when, pruned=False)
            self._delete()
            db.commit()
        elif sr.changed:
            db.execute("UPDATE `mailboxes`"
                       " SET `side1`=?, `side2`=?, `first_mood`=?"
                       " WHERE `app_id`=? AND `id`=?",
                       (sr.side1, sr.side2, mood,
                        self._app_id, self._mailbox_id))
            db.commit()

    def _delete(self):
        # requires caller to db.commit()
        self._db.execute("DELETE FROM `mailboxes`"
                         " WHERE `app_id`=? AND `id`=?",
                         (self._app_id, self._mailbox_id))
        self._db.execute("DELETE FROM `messages`"
                         " WHERE `app_id`=? AND `mailbox_id`=?",
                         (self._app_id, self._mailbox_id))

        # Shut down any listeners, just in case they're still lingering
        # around.
        for (send_f, stop_f) in self._listeners.values():
            stop_f()

        self._app.free_mailbox(self._mailbox_id)

    def _summarize_and_store(self, row, num_sides, second_mood, delete_time,
                             pruned):
        u = self._summarize(row, num_sides, second_mood, delete_time, pruned)
        self._db.execute("INSERT INTO `mailbox_usage`"
                         " (`app_id`, "
                         "  `started`, `total_time`, `waiting_time`, `result`)"
                         " VALUES (?, ?,?,?,?)",
                         (self._app_id,
                          u.started, u.total_time, u.waiting_time, u.result))

    def _summarize(self, row, num_sides, second_mood, delete_time, pruned):
        started = row["started"]
        if self._blur_usage:
            started = self._blur_usage * (started // self._blur_usage)
        waiting_time = None
        if row["second"]:
            waiting_time = row["second"] - row["started"]
        total_time = delete_time - row["started"]

        if num_sides == 0:
            result = u"quiet"
        elif num_sides == 1:
            result = u"lonely"
        else:
            result = u"happy"

        moods = set([row["first_mood"], second_mood])
        if u"lonely" in moods:
            result = u"lonely"
        if u"errory" in moods:
            result = u"errory"
        if u"scary" in moods:
            result = u"scary"
        if pruned:
            result = u"pruney"
        if row["crowded"]:
            result = u"crowded"

        return Usage(started=started, waiting_time=waiting_time,
                     total_time=total_time, result=result)

    def is_idle(self):
        if self._listeners:
            return False
        c = self._db.execute("SELECT `server_rx` FROM `messages`"
                             " WHERE `app_id`=? AND `mailbox_id`=?"
                             " ORDER BY `server_rx` DESC LIMIT 1",
                             (self._app_id, self._mailbox_id))
        rows = c.fetchall()
        if not rows:
            return True
        old = time.time() - CHANNEL_EXPIRATION_TIME
        if rows[0]["server_rx"] < old:
            return True
        return False

    def _shutdown(self):
        # used at test shutdown to accelerate client disconnects
        for (send_f, stop_f) in self._listeners.values():
            stop_f()

class AppNamespace:
    def __init__(self, db, welcome, blur_usage, log_requests, app_id):
        self._db = db
        self._welcome = welcome
        self._blur_usage = blur_usage
        self._log_requests = log_requests
        self._app_id = app_id
        self._mailboxes = {}

    def get_nameplate_ids(self):
        db = self._db
        # TODO: filter this to numeric ids?
        c = db.execute("SELECT DISTINCT `id` FROM `nameplates`"
                       " WHERE `app_id`=?", (self._app_id,))
        return set([row["id"] for row in c.fetchall()])

    def _find_available_nameplate_id(self):
        claimed = self.get_nameplate_ids()
        for size in range(1,4): # stick to 1-999 for now
            available = set()
            for id_int in range(10**(size-1), 10**size):
                id = u"%d" % id_int
                if id not in claimed:
                    available.add(id)
            if available:
                return random.choice(list(available))
        # ouch, 999 currently claimed. Try random ones for a while.
        for tries in range(1000):
            id_int = random.randrange(1000, 1000*1000)
            id = u"%d" % id_int
            if id not in claimed:
                return id
        raise ValueError("unable to find a free nameplate-id")

    def allocate_nameplate(self, side, when):
        nameplate_id = self._find_available_nameplate_id()
        mailbox_id = self.claim_nameplate(nameplate_id, side, when)
        del mailbox_id # ignored, they'll learn it from claim()
        return nameplate_id

    def claim_nameplate(self, nameplate_id, side, when):
        # when we're done:
        # * there will be one row for the nameplate
        #  * side1 or side2 will be populated
        #  * started or second will be populated
        #  * a mailbox id will be created, but not a mailbox row
        #    (ids are randomly unique, so we can defer creation until 'open')
        assert isinstance(nameplate_id, type(u"")), type(nameplate_id)
        assert isinstance(side, type(u"")), type(side)
        db = self._db
        row = db.execute("SELECT * FROM `nameplates`"
                         " WHERE `app_id`=? AND `id`=?",
                         (self._app_id, nameplate_id)).fetchone()
        if row:
            mailbox_id = row["mailbox_id"]
            try:
                sr = add_side(row, side)
            except CrowdedError:
                db.execute("UPDATE `nameplates` SET `crowded`=?"
                           " WHERE `app_id`=? AND `id`=?",
                           (True, self._app_id, nameplate_id))
                db.commit()
                raise
            if sr.changed:
                db.execute("UPDATE `nameplates` SET"
                           " `side1`=?, `side2`=?, `updated`=?, `second`=?"
                           " WHERE `app_id`=? AND `id`=?",
                           (sr.side1, sr.side2, when, when,
                            self._app_id, nameplate_id))
        else:
            if self._log_requests:
                log.msg("creating nameplate#%s for app_id %s" %
                        (nameplate_id, self._app_id))
            mailbox_id = generate_mailbox_id()
            db.execute("INSERT INTO `nameplates`"
                       " (`app_id`, `id`, `mailbox_id`, `side1`, `crowded`,"
                       "  `updated`, `started`)"
                       " VALUES(?,?,?,?,?, ?,?)",
                       (self._app_id, nameplate_id, mailbox_id, side, False,
                        when, when))
        db.commit()
        return mailbox_id

    def release_nameplate(self, nameplate_id, side, when):
        # when we're done:
        # * in the nameplate row, side1 or side2 will be removed
        # * if the nameplate is now unused:
        #  * mailbox.nameplate_closed will be populated
        #  * the nameplate row will be removed
        assert isinstance(nameplate_id, type(u"")), type(nameplate_id)
        assert isinstance(side, type(u"")), type(side)
        db = self._db
        row = db.execute("SELECT * FROM `nameplates`"
                         " WHERE `app_id`=? AND `id`=?",
                         (self._app_id, nameplate_id)).fetchone()
        if not row:
            return
        sr = remove_side(row, side)
        if sr.empty:
            db.execute("DELETE FROM `nameplates`"
                       " WHERE `app_id`=? AND `id`=?",
                       (self._app_id, nameplate_id))
            self._summarize_nameplate_and_store(row, when, pruned=False)
            db.commit()
        elif sr.changed:
            db.execute("UPDATE `nameplates`"
                       " SET `side1`=?, `side2`=?, `updated`=?"
                       " WHERE `app_id`=? AND `id`=?",
                       (sr.side1, sr.side2, when,
                        self._app_id, nameplate_id))
            db.commit()

    def _summarize_nameplate_and_store(self, row, delete_time, pruned):
        # requires caller to db.commit()
        u = self._summarize_nameplate_usage(row, delete_time, pruned)
        self._db.execute("INSERT INTO `nameplate_usage`"
                         " (`app_id`,"
                         " `started`, `total_time`, `waiting_time`, `result`)"
                         " VALUES (?, ?,?,?,?)",
                         (self._app_id,
                          u.started, u.total_time, u.waiting_time, u.result))

    def _summarize_nameplate_usage(self, row, delete_time, pruned):
        started = row["started"]
        if self._blur_usage:
            started = self._blur_usage * (started // self._blur_usage)
        waiting_time = None
        if row["second"]:
            waiting_time = row["second"] - row["started"]
        total_time = delete_time - row["started"]
        result = u"lonely"
        if row["second"]:
            result = u"happy"
        if pruned:
            result = u"pruney"
        if row["crowded"]:
            result = u"crowded"
        return Usage(started=started, waiting_time=waiting_time,
                     total_time=total_time, result=result)

    def _prune_nameplate(self, row, delete_time):
        # requires caller to db.commit()
        db = self._db
        db.execute("DELETE FROM `nameplates` WHERE `app_id`=? AND `id`=?",
                   (self._app_id, row["id"]))
        self._summarize_nameplate_and_store(row, delete_time, pruned=True)
        # TODO: make a Nameplate object, keep track of when there's a
        # websocket that's watching it, don't prune a nameplate that someone
        # is watching, even if they started watching a long time ago

    def prune_nameplates(self, old):
        db = self._db
        for row in db.execute("SELECT * FROM `nameplates`"
                              " WHERE `updated` < ?",
                              (old,)).fetchall():
            self._prune_nameplate(row)
        count = db.execute("SELECT COUNT(*) FROM `nameplates`").fetchone()[0]
        return count

    def open_mailbox(self, mailbox_id, side, when):
        assert isinstance(mailbox_id, type(u"")), type(mailbox_id)
        db = self._db
        if not mailbox_id in self._mailboxes:
            if self._log_requests:
                log.msg("spawning #%s for app_id %s" % (mailbox_id,
                                                        self._app_id))
            db.execute("INSERT INTO `mailboxes`"
                       " (`app_id`, `id`, `side1`, `crowded`, `started`)"
                       " VALUES(?,?,?,?,?)",
                       (self._app_id, mailbox_id, side, False, when))
            db.commit() # XXX
            # mailbox.open() does a SELECT to find the old sides
            self._mailboxes[mailbox_id] = Mailbox(self, self._db,
                                                  self._blur_usage,
                                                  self._log_requests,
                                                  self._app_id, mailbox_id)
        mailbox = self._mailboxes[mailbox_id]
        mailbox.open(side, when)
        db.commit()
        return mailbox

    def free_mailbox(self, mailbox_id):
        # called from Mailbox.delete_and_summarize(), which deletes any
        # messages

        if mailbox_id in self._mailboxes:
            self._mailboxes.pop(mailbox_id)
        #if self._log_requests:
        #    log.msg("freed+killed #%s, now have %d DB mailboxes, %d live" %
        #            (mailbox_id, len(self.get_claimed()), len(self._mailboxes)))

    def prune_mailboxes(self, old):
        # For now, pruning is logged even if log_requests is False, to debug
        # the pruning process, and since pruning is triggered by a timer
        # instead of by user action. It does reveal which mailboxes were
        # present when the pruning process began, though, so in the log run
        # it should do less logging.
        log.msg("  channel prune begins")
        # a channel is deleted when there are no listeners and there have
        # been no messages added in CHANNEL_EXPIRATION_TIME seconds
        mailboxes = set(self.get_claimed()) # these have messages
        mailboxes.update(self._mailboxes) # these might have listeners
        for mailbox_id in mailboxes:
            log.msg("   channel prune checking %d" % mailbox_id)
            channel = self.get_channel(mailbox_id)
            if channel.is_idle():
                log.msg("   channel prune expiring %d" % mailbox_id)
                channel.delete_and_summarize() # calls self.free_channel
        log.msg("  channel prune done, %r left" % (self._mailboxes.keys(),))
        return bool(self._mailboxes)

    def _shutdown(self):
        for channel in self._mailboxes.values():
            channel._shutdown()

class Rendezvous(service.MultiService):
    def __init__(self, db, welcome, blur_usage):
        service.MultiService.__init__(self)
        self._db = db
        self._welcome = welcome
        self._blur_usage = None
        log_requests = blur_usage is None
        self._log_requests = log_requests
        self._apps = {}
        t = internet.TimerService(EXPIRATION_CHECK_PERIOD, self.prune)
        t.setServiceParent(self)

    def get_welcome(self):
        return self._welcome
    def get_log_requests(self):
        return self._log_requests

    def get_app(self, app_id):
        assert isinstance(app_id, type(u""))
        if not app_id in self._apps:
            if self._log_requests:
                log.msg("spawning app_id %s" % (app_id,))
            self._apps[app_id] = AppNamespace(self._db, self._welcome,
                                             self._blur_usage,
                                             self._log_requests, app_id)
        return self._apps[app_id]

    def prune(self, old=None):
        # As with AppNamespace.prune_old_mailboxes, we log for now.
        log.msg("beginning app prune")
        if old is None:
            old = time.time() - CHANNEL_EXPIRATION_TIME
        c = self._db.execute("SELECT DISTINCT `app_id` FROM `messages`")
        apps = set([row["app_id"] for row in c.fetchall()]) # these have messages
        apps.update(self._apps) # these might have listeners
        for app_id in apps:
            log.msg(" app prune checking %r" % (app_id,))
            app = self.get_app(app_id)
            still_active = app.prune_nameplates(old) + app.prune_mailboxes(old)
            if not still_active:
                log.msg("prune pops app %r" % (app_id,))
                self._apps.pop(app_id)
        log.msg("app prune ends, %d remaining apps" % len(self._apps))

    def stopService(self):
        # This forcibly boots any clients that are still connected, which
        # helps with unit tests that use threads for both clients. One client
        # hits an exception, which terminates the test (and .tearDown calls
        # stopService on the relay), but the other client (in its thread) is
        # still waiting for a message. By killing off all connections, that
        # other client gets an error, and exits promptly.
        for app in self._apps.values():
            app._shutdown()
        return service.MultiService.stopService(self)