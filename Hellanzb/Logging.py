"""

Logging - hellanzb's logging facility. Ties in with python's logging system, with a bunch
of added locks to support interrupting nzbget scroll with other log messages. This is
pretty elaborate for a basic app like hellanzb, but I felt like playing with threads and
looking into python's logging system. Hoho.

@author pjenvey
"""
import logging, logging.handlers, sys, time, xmlrpclib
from logging import StreamHandler
from threading import Condition, Lock, Thread
from Util import *

class ScrollableHandler(StreamHandler):
    """ ScrollableHandler is a StreamHandler that specially handles scrolling (log messages at the SCROLL level). It allows you to temporarily interrupt the constant scroll with other log messages of different levels. It also slightly pauses the scroll output, giving you time to read the message  """
    # the SCROLL level (a class var)
    SCROLL = 11

    def handle(self, record):
        """ The 'scroll' level is a constant scroll that can be interrupted. This interruption is
done via a lock (ScrollableHandler.scrollLock) -- if this Handler is logging a scroll
record, it will only emit the record if the handler can immediately acquire the scroll
lock. If it fails to acquire the lock it will throw away the scroll record. """
        rv = self.filter(record)
        if rv:

            if record.levelno == ScrollableHandler.SCROLL:
                # Only print scroll if we can immediately acquire the scroll lock
                if ScrollableHandler.scrollLock.acquire(False):

                    # got the lock -- scroll is now on and no longer interrupted
                    ScrollableHandler.scrollInterrupted = False

                    try:
                        self.emitSynchronized(record)
                    finally:
                        ScrollableHandler.scrollLock.release()

                else:
                    # no scroll for you
                    return rv
            else:
                # If scroll is on, queue the message for the ScrollInterrupter
                if ScrollableHandler.scrollFlag:
                    self.queueScrollInterrupt(record)

                else:
                    # otherwise if scroll isn't on, just log the message normally
                    self.emitSynchronized(record)
                            
        return rv

    def emitSynchronized(self, record):
        """ Write a log message atomically """
        self.acquire()
        try:
            self.emit(record)
        finally:
            self.release()

    def queueScrollInterrupt(self, record):
        """ Lock the list of pending interrupt records, then notify the interrupter it has work to
do """
        # Give the interrupter a reference back to where it will actually emit the log
        # record (ourself)
        record.scrollableHandler = self
        
        try:
            ScrollInterrupter.pendingMonitor.acquire()
            ScrollInterrupter.pendingLogRecords.append(record)
            ScrollInterrupter.pendingMonitor.notify()
            ScrollInterrupter.notifiedMonitor = True
        finally:
            ScrollInterrupter.pendingMonitor.release()

class ScrollInterrupter(Thread):
    """ The scroll interrupter handles printing out a message and pausing the scroll for a
short time after those messages are printed. Even though this is only a brief pause, we
don't want logging to block while this pause happens. Thus the pause is handled by this
permanent thread. This is modeled after a logging.Handler, but it does not extend it --
it's more of a child helper handler of ScrollableHandler, than an offical logging Handler
"""
    def acquire(self, record):
        """ Acquire the ScrollableHandler lock (log a message atomically via pythons logging) """ 
        record.scrollableHandler.acquire()
        
    def release(self, record):
        """ Release the ScrollableHandler lock """
        record.scrollableHandler.release()

    def format(self, record):
        """ Add spaces to the scroll interrupting log message, unless we've already interrupted
scroll and already added the spaces """
        if not ScrollableHandler.scrollInterrupted:
            record.msg = '\n\n\n\n' + record.msg + '\n\n'
            ScrollableHandler.scrollInterrupted = True
        else:
            record.msg = record.msg + '\n\n'
        return record

    def handle(self, record):
        """ Handle the locking required to atomically log a message
        @see Handler.handle(self, record) """
        # atomically log the the message.
        self.acquire(record)
        try:
            self.emit(record)
        finally:
            self.release(record)

    def emit(self, record):
        """ Do the actual logging work
        @see Handler.emit(self, record)"""
        record = self.format(record)
        self.emitToParent(record)
        
    def emitToParent(self, record):
        """ Pass the log message back to the ScrollableHandler """
        record.scrollableHandler.emit(record)

    def wait(self, timeout = None):
        """ Wait for notification of new log messages """
        # FIXME: this lock should never really be released by this function, except by
        # wait. does that fly with the temporary wait below?
        ScrollInterrupter.pendingMonitor.acquire()
        
        result = ScrollInterrupter.pendingMonitor.wait(timeout)
        if timeout == None and ScrollInterrupter.notifiedMonitor:
           ScrollInterrupter.notifiedMonitor = False

        ScrollInterrupter.pendingMonitor.release()

        return result

    def checkShutdown(self):
        """ Simply return false causing this thread to die if we're shutting down """
        if Hellanzb.shutdown:
            raise SystemExit(Hellanzb.SHUTDOWN_CODE)
        return False

    def run(self):
        """ do the work and allow the thread to shutdown cleanly under all circumstances """
        try:
            self.waitLoop()
        except (AttributeError, NameError), e:
            # this happens during shutdown
            pass
        except SystemExit:
            pass
        except Exception, e:
            print 'Error in ScrollInterrupter: ' + str(e)

    def lockScrollOutput(self):
        """ Prevent scroll output """
        ScrollableHandler.scrollLock.acquire()
        
    def releaseScrollOutput(self):
        """ Continue scroll output """
        ScrollableHandler.scrollLock.release()

    # FIXME: change self.wait to self.waitForRecord
    # make the wait(timeout) use the same function. shouldn't i just acquire the pending
    # monitor lock at the beginning and never release it?

    # FIXME: problem with this function is, you can definitely have a case where you were
    # notified and weren't wait()ing. you sort of have to manually look for any pending
    # records at the beginning of the loop or if i can never release the lock
    def waitLoop(self):
        """ wait for scroll interrupts, then break the scroll to print them """
        # hadQuickWait explained below. basically toggles whether or not we release the
        # scroll locks during the loop
        hadQuickWait = False

        # Continue waiting for scroll interrupts until shutdown
        while not self.checkShutdown():

            # See below
            if not hadQuickWait:
                # Wait until we're notified of a new log message
                self.wait()
                
                # We've been notified -- block the scroll output,
                ScrollableHandler.scrollLock.acquire()
                # and lock the data structure containing the new log messages
                ScrollInterrupter.pendingMonitor.acquire()
            else:
                # release the locks next time around unless we hadQuickWait again (see
                # below)
                hadQuickWait = False
            
            # copy all the new log messages and remove them from the pending list
            #records = self.scrollableHandler.scrollInterruptRecords[:]
            records = ScrollInterrupter.pendingLogRecords[:]
            ScrollInterrupter.pendingLogRecords = []

            # Print the new messages.
            for record in records:
                self.handle(record)
                
            # Now that we've printed the log messages, we want to continue blocking the
            # scroll output for a few seconds. However if we're notified of a new pending
            # log (scroll interrupt) messages, we want to print it immediately, and
            # restart the 3 second count
            ScrollInterrupter.pendingMonitor.wait(Hellanzb.Logging.SCROLL_INTERRUPT_WAIT)

            # wait() won't tell us whether or not we were actually notified. If we were,
            # this would have been set to true (while notify() acquired the lock during
            # our wait())
            if ScrollInterrupter.notifiedMonitor:
                ScrollInterrupter.notifiedMonitor = False
                # We caught new log messages and want to continue interrutping the
                # scroll. So we won't release the locks the next time around the loop
                hadQuickWait = True
                
            else:
                # We waited a few seconds and weren't notified of new messages. Let the
                # scroll continue
                ScrollInterrupter.pendingMonitor.release()
                
                ScrollableHandler.scrollLock.release()

def warn(message):
    """ Log a message at the warning level """
    Hellanzb.logger.warn(message)

def error(message, exception = None):
    """ Log a message at the error level. Optionally log exception information """
    message = message
    
    if exception != None:
        if isinstance(exception, Exception):
            message = message + ': ' + getLocalClassName(exception.__class__) + ': ' + str(exception)
        
    Hellanzb.logger.error(message)

def info(message):
    """ Log a message at the info level """
    Hellanzb.logger.info(message)

def debug(message):
    """ Log a message at the debug level """
    Hellanzb.logger.debug(message)

def scroll(message):
    """ Log a message at the scroll level """
    Hellanzb.logger.log(ScrollableHandler.SCROLL, message)
    # Somehow the scroll locks end up getting blocked unless their consumers pause as
    # short as around 1/100th of a milli every loop. You might notice this delay when
    # nzbget scrolling looks like a slightly different FPS from within hellanzb than
    # running it directly
    time.sleep(.00001)

def growlNotify(type, title, description, sticky):
    """ send a message to the growl daemon via an xmlrpc proxy """
    # NOTE: growl doesn't tie in with logging yet because all it's sublevels/args makes it
    # not play well with the rest of the logging.py
    
    # FIXME: should validate the server information on startup, and catch connection
    # refused errors here
    if not Hellanzb.ENABLE_GROWL_NOTIFY:
        return

    # NOTE: we probably want this in it's own thread to be safe, i can see this easily
    # deadlocking for a bit on say gethostbyname()
    # AND we could have a LOCAL_GROWL option for those who might run hellanzb on os x
    serverUrl = 'http://' + Hellanzb.SERVER + '/'
    server = xmlrpclib.Server(serverUrl)

    # If for some reason, the XMLRPC server ain't there no more, this will blow up
    # so we put it in a try/except block
    try:
        server.notify(type, title, description, sticky)
    except:
        return
    
def scrollBegin():
    """ Let the logger know we're beginning to scroll """
    ScrollableHandler.scrollFlag = True
    ScrollableHandler.scrollLock = Lock()

def scrollEnd():
    """ Let the logger know we're done scrolling """
    ScrollableHandler.scrollFlag = False
    del ScrollableHandler.scrollLock

def init():
    """ Setup logging """
    logging.addLevelName(ScrollableHandler.SCROLL, 'SCROLL')

    Hellanzb.logger = logging.getLogger('hellanzb')
    Hellanzb.logger.setLevel(ScrollableHandler.SCROLL)

    # Filter for stdout -- log warning and below
    class OutFilter(logging.Filter):
        def filter(self, record):
            if record.levelno > logging.WARNING:
                return False
            elif record.levelno == logging.DEBUG and not Hellanzb.DEBUG_MODE:
                return False
            return True
    
    outHdlr = ScrollableHandler(sys.stdout)
    #outHdlr.setLevel(ScrollableHandler.SCROLL)
    outHdlr.addFilter(OutFilter())

    errHdlr = ScrollableHandler(sys.stderr)
    errHdlr = logging.StreamHandler(sys.stderr)
    errHdlr.setLevel(logging.ERROR)
    
    Hellanzb.logger.addHandler(outHdlr)
    Hellanzb.logger.addHandler(errHdlr)

    # FIXME: could move this to config file
    # How many seconds to delay the scroll for
    Hellanzb.Logging.SCROLL_INTERRUPT_WAIT = 5
    # 2 is for testing
    #Hellanzb.Logging.SCROLL_INTERRUPT_WAIT = 2


    # Whether or not scroll mode is on
    ScrollableHandler.scrollFlag = False
    # Whether or not there is currently output interrupting the scroll
    ScrollableHandler.scrollInterrupted = True
    # the lock that allows us interrupt scroll (is initialized via scrollEnd())
    ScrollableHandler.scrollLock = None

    # For communication to the scroll interrupter
    # FIXME: could put these in interrupter cstrctr. interrupter can throw an exception if
    # its instantiated twice
    ScrollInterrupter.pendingMonitor = Condition(Lock())
    ScrollInterrupter.notifiedMonitor = False
    ScrollInterrupter.pendingLogRecords = []

    # Start the thread after initializing all those variables (singleton)
    scrollInterrupter = ScrollInterrupter()
    scrollInterrupter.start()

def initLogFile():
    """ Initialize the log file. This has to be done after the config is loaded """

    class LogFileFilter(logging.Filter):
        def filter(self, record):
            if record.levelno == ScrollableHandler.SCROLL:
                return False
            return True
    
    # FIXME: should check if Hellanzb.LOG_FILE is set first
    fileHdlr = logging.handlers.RotatingFileHandler(Hellanzb.LOG_FILE)
    fileHdlr.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    fileHdlr.addFilter(LogFileFilter())
    
    Hellanzb.logger.addHandler(fileHdlr)