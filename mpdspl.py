#! /usr/bin/env python
#
# A script to create smart playlists, with a variety of criteria, out of an
# MPD database.
#
# Authors:
#   Sebastien Delafond <sdelafond@gmail.com>
#   original implementation by Michael Walker <mike@barrucadu.co.uk>
#
# This code is licensed under the GPL v3, or any later version at your choice.

import cPickle, datetime, operator, optparse
import os, os.path, sqlite3, sys, re, textwrap, time

DEFAULT_MDP_CONFIG_FILE = "/etc/mpd.conf"

# There is an environmental variable XDG_CACHE_HOME which specifies where to
# save cache files. However, if not set, a default of ~/.cache should be used.
DEFAULT_CACHE_FILE = os.environ.get('XDG_CACHE_HOME',
                                    os.path.join(os.environ['HOME'], ".cache"))
DEFAULT_CACHE_FILE = os.path.expanduser(os.path.join(DEFAULT_CACHE_FILE,
                                                     "mpdspl/mpddb.cache"))

# $XDG_DATA_HOME specifies where to save data files, in our case a record of
# playlists which have been created. If unset a default of ~/.local/share
# should be used.
DEFAULT_DATA_DIR = os.environ.get('XDG_DATA_HOME',
                                  os.path.join(os.environ['HOME'], ".local/share/"))
DEFAULT_DATA_DIR = os.path.expanduser(os.path.join(DEFAULT_DATA_DIR,
                                                   "mpdspl"))

KEYWORDS = {"ar" : ("Artist", "Artist"),
            "al" : ("Album", "Album"),
            "ti" : ("Title", "Title"),
            "tn" : ("Track", "Track number"),
            "ge" : ("Genre", "Genre"),
            "ye" : ("Date", "Track year"),
            "le" : ("Time", "Track duration (in seconds)"),
            "fp" : ("file", "File full path"),
            "fn" : ("key", "File name"),
            "mt" : ("mtime", "File modification time"),
            "ra" : ("Rating", "Track rating") }

class AbstractRule:
    def __init__(self, key, operator, delimiter, value, flags):
        self.key = key
        self.operator = operator
        self.delimiter = delimiter
        self.value = value
        if flags:
            self.flags = tuple(flags)
        else:
            self.flags = ()
        self.negate = 'n' in self.flags

    def __repr__(self):
        return "%(key)s%(operator)s%(delimiter)s%(value)s%(delimiter)s flags=%(flags)s" % self.__dict__

    def getOperator(self):
        return self.OPERATORS[self.operator]
    
    def match(self, track):
        value = getattr(track, KEYWORDS[self.key][0].lower())
        matched = self.__match__(value)

        if self.negate:
            matched = not matched
        return matched
    
class RegexRule(AbstractRule):
    """ Search according to a regex, for instance:
               contains foo                     -->   =/foo/
               contains bar, case-insensitive   -->   =/bar/i """
    
    OPERATORS = { '=' : re.search }
    FLAGS = { 'i' : re.IGNORECASE,
              'l' : re.LOCALE }
    
    def __init__(self, key, operator, delimiter, value, flags):
        AbstractRule.__init__(self, key, operator,
                              delimiter, value, flags)
        self.reFlags = 0
        for reFlag in self.flags:
            self.reFlags |= self.FLAGS[reFlag]

    def __match__(self, value):
        return self.getOperator()(self.value, value, self.reFlags)
        
class TimeDeltaRule(AbstractRule):
    """ Match according to a timedelta, for instance:
               in the last 3 days   -->   <%3days%
               before last month    -->   >%1month%
               3 years ago          -->   =%3years% """
    
    OPERATORS = { '=' : operator.eq,
                  '<' : operator.le,
                  '>' : operator.ge }
    
    TIME_DELTA_REGEX = r'(?P<number>\d+)\s*(?P<unit>\w+)'

    def __init__(self, key, operator, delimiter, value, flags):
        AbstractRule.__init__(self, key, operator,
                              delimiter, value, flags)
        
        m = re.match(self.TIME_DELTA_REGEX, self.value)
        if not m:
            raise Exception("Could not parse duration")
        d = m.groupdict()
        self.number = int(d['number'])
        self.unit = d['unit'].lower()
        if not self.unit.endswith('s'):
            self.unit += 's'

        self.timeDelta = datetime.timedelta(**{self.unit : self.number})
        self.value = self.timeDelta.seconds

    def __match__(self, value):
        return self.getOperator()(int(value), self.value)

class TimeStampRule(AbstractRule):
    """ Match according to a timestamp, for instance:
               before 2010-01-02   -->   <@2010-01-02@
               after  2009-12-20   -->   >@2009-12-20@
               on     2009-11-18   -->   =@2009-11-18@ """
    
    OPERATORS = { '=' : operator.eq,
                  '<' : operator.le,
                  '>' : operator.ge }
    
    TIME_STAMP_FORMAT = '%Y-%m-%d'

    def __init__(self, key, operator, delimiter, value, flags):
        AbstractRule.__init__(self, key, operator,
                              delimiter, value, flags)
        
        ts = time.strptime(self.value, self.TIME_STAMP_FORMAT)
        self.value = time.mktime(ts)

    def __match__(self, value):
        # round down to the precision of TIME_STAMP_FORMAT before comparing
        value = time.gmtime(float(value)) # in seconds since epoch
        value = time.strftime(self.TIME_STAMP_FORMAT, value)        
        value = time.mktime(time.strptime(value, self.TIME_STAMP_FORMAT))
        return self.getOperator()(value, self.value)

class RuleFactory:
    DELIMITER_TO_RULE = { '/' : RegexRule,
                          '%' : TimeDeltaRule,
                          '@' : TimeStampRule }

    @staticmethod
    def getRule(ruleString):
        m = re.match(r'(?P<key>\w+)(?P<operator>.)(?P<delimiter>[' +
                     ''.join(RuleFactory.DELIMITER_TO_RULE.keys()) +
                     r'])(?P<value>.+)\3(?P<flags>\w+)?',
                     ruleString)
        if not m:
            raise Exception("Could not parse rule '%s'" % (ruleString,))

        d = m .groupdict()
        ruleClass = RuleFactory.DELIMITER_TO_RULE[d['delimiter']]
        return ruleClass(**d)

    @staticmethod
    def help():
        s = ""
        for d, r in RuleFactory.DELIMITER_TO_RULE.iteritems():
            s += "          '%s' -> %s\n" % (d, r.__doc__)
        return s

class Playlist:
    REGEX = re.compile(r'\s+')

    def __init__(self, name, ruleString):
        self.name = name
        self.rules = [ RuleFactory.getRule(r)
                       for r in self.REGEX.split(ruleString) ]
        self.tracks = [] # tracks matching the rules; empty for now

    def findMatchingTracks(self, tracks):
        self.tracks = []
    
        for track in tracks.values():
            toAdd = True
            for rule in self.rules:
                if not rule.match(track): # Add the track if appropriate
                    toAdd = False
                    break

            if toAdd:
                self.tracks.append(track)

        self.setM3u()

    def setM3u(self):
        self.m3u = '\n'.join([ track.file for track in self.tracks ]) + '\n'

    def getSaveFile(self, dataDir):
        return os.path.join(dataDir, self.name)

    def save(self, dataDir):
        savegubbage(self, self.getSaveFile(dataDir))

    def getM3uPath(self, playlistDir):
        return os.path.join(playlistDir, self.name + ".m3u")

    def writeM3u(self, playlistDir):
        filePath = self.getM3uPath(playlistDir)
        print "Saving playlist '%s' to '%s'" % (playlist.name, filePath)
        open(filePath, 'w').write(self.m3u)

class Track:
    def __init__(self):
        # create a track object with only empty attributes
        for key in KEYWORDS.values():
            setattr(self, key[0].lower(), "")

        
class IndentedHelpFormatterWithNL(optparse.IndentedHelpFormatter):
    """ So optparse doesn't mangle our help description. """
    def format_description(self, description):
        if not description: return ""
        desc_width = self.width - self.current_indent
        indent = " "*self.current_indent
        bits = description.split('\n')
        formatted_bits = [ textwrap.fill(bit,
                                         desc_width,
                                         initial_indent=indent,
                                         subsequent_indent=indent)
                           for bit in bits]
        result = "\n".join(formatted_bits) + "\n"
        return result 

def parseargs(args):
    parser = optparse.OptionParser(formatter=IndentedHelpFormatterWithNL(),
                                   description="""Playlist ruleset:
        Each ruleset is made of several rules, separated by spaces.
        Each rule is made of a keyword, an operator, a value to match
        surrounded by delimiters, and several optional flags influencing the
        match.
        There are """ + str(len(RuleFactory.DELIMITER_TO_RULE.keys())) + \
        """ types of rules, each defined by a specific delimiter:\n\n""" + \

        RuleFactory.help() + \

        """        These available keywords are:
""" + \

        '\n'.join([ "            " + k + " : " + v[1] for k, v in KEYWORDS.iteritems() ]) + \

        """

        For example, a rule for all tracks by 'Fred' or 'George', which have a
        title containing (case-insensitive) 'the' and 'and', which don't
        include the word 'when' (case-insensitive), and whose modification
        time was in the last 3 days would be written:

          ar=/(Fred|George)/ ti=/(the.*and|and.*the)/i ti=/when/i mt<%3days%
          
    Notes:
        Paths specified in the MPD config file containing a '~' will have the
        '~'s replaced by the user MPD runs as..""")

    parser.add_option("-f", "--force-update", dest="forceUpdate",
                      action="store_true", default=False,
                      help="Force an update of the cache file and any playlists")

    parser.add_option("-C", "--cache-file", dest="cacheFile",
                      default=DEFAULT_CACHE_FILE,
                      help="Location of the cache file", metavar="FILE")

    parser.add_option("-D", "--data-dir", dest="dataDir",
                      default=DEFAULT_DATA_DIR,
                      help="Location of the data directory (where we save playlist info)",
                      metavar="DIR")

    parser.add_option("-d", "--database-file", dest="dbFile", 
                      help="Location of the MPD database file",
                      metavar="FILE")

    parser.add_option("-s", "--sticker-file", dest="stickerFile",
                      help="Location of the MPD sticker file( holding ratings)",
                      metavar="FILE")

    parser.add_option("-c", "--config-file", dest="configFile",
                      default=DEFAULT_MDP_CONFIG_FILE,
                      help="Location of the MPD config file",
                      metavar="FILE")

    parser.add_option("-p", "--playlist-dir", dest="playlistDirectory",
                      help="Location of the MPD playlist directory",
                      metavar="DIR")

    parser.add_option("-u", "--user", dest="mpdUser",
                      help="User MPD runs as", metavar="USER")

    parser.add_option("-n", "--new-playlist", dest="playlists",
                      action="append", default=[], nargs=2,
                      help="Create a new playlist",
                      metavar="NAME 'RULESET'")

    parser.add_option("-o", "--output-only", dest="simpleOutput",
                      action="store_true", default=False,
                      help="Only print the final track list to STDOUT")

    options, args = parser.parse_args(args)

    # we'll use dataDir=None to indicate we want simpleOutput
    if options.simpleOutput:
        options.dataDir = None

    # go from ((name,rule),(name1,rule1),...) to {name:rule,name1:rule1,...}
    playlists = []
    for name, ruleSet in options.playlists:
        playlists.append(Playlist(name, ruleSet))
    options.playlists = playlists

    configDict = parsempdconf(os.path.expanduser(options.configFile),
                              options.mpdUser)

    # CL arguments take precedence over config file settings
    for key in configDict:
        if key in dir(options) and getattr(options, key):
            configDict[key] = getattr(options, key)

    return options.forceUpdate, options.cacheFile, options.dataDir, \
           configDict['dbFile'], configDict['stickerFile'], \
           configDict['playlistDirectory'], options.playlists

def _underscoreToCamelCase(s):
    tokens = s.split('_')
    s = tokens[0]
    for token in tokens[1:]:
        s += token.capitalize()
    return s
    
# Grabbing stuff from the MPD config, a very important step
def parsempdconf(configFile, user = None):
    configDict = {}
    for line in open(configFile, "r"):
        line = line.strip()
        if line and not re.search(r'[#{}]', line):
            key, value = re.split(r'\s+', line, 1)

            key = _underscoreToCamelCase(key)

            value = re.sub(r'(^"|"$)', '', value)

            # account for ~/ in mpd.conf
            if value == '~' or value.count('~/') > 0: # FIXME: others ?
                if user:
                    value = value.replace('~', user)
                else:
                    value = os.path.expanduser(value)
                    
            configDict[key] = value

    return configDict

# A function to parse a MPD database and make a huge list of tracks
def parsedatabase(dbFile):
    tracks = {}
    parsing = False

    track = None
    for line in open(dbFile, "r"):
        # For every line in the database, remove any whitespace at the
        # beginning and end so the script isn't buggered.
        line = line.strip()

        # If entering a songList, start parsing
        if line == "songList begin":
            parsing = True
            continue
        if line == "songList end":
            parsing = False
            continue
        
        if parsing:
            if line.startswith("key: "):
                if track is not None: # save the previous one
                    tracks[track.file] = track
                track = Track() # create a new one

            key, value = line.split(": ", 1)
            setattr(track, key.lower(), value)

    return tracks

def parseStickerDB(stickerFile, tracks):
    conn = sqlite3.connect(stickerFile)

    curs = conn.cursor()

    curs.execute('SELECT * FROM sticker WHERE type=? and name=?',
                 ("song", "rating"))

    for row in curs:
        filePath = row[1]
        if filePath in tracks:
            tracks[filePath].rating = row[3]

    return tracks

# Save some random gubbage to a file
def savegubbage(data, path):
    if not os.path.isdir(os.path.dirname(path)):
        os.mkdir(os.path.dirname(path))

    cPickle.dump(data, open(path, "wb"))

    # We might be running as someone other than the user, so make the file writable
    os.chmod(path, 438)

def loadgubbage(path):
    return cPickle.load(open(path, "rb"))

# Parse some options!
forceUpdate, cacheFile, dataDir, dbFile, stickerFile, playlistDir, playlists = parseargs(sys.argv[1:])
# FIXME: create non-existing directories

# Check that the database is actually there before attempting to do stuff with it.
if not os.path.isfile(dbFile):
    sys.stderr.write("The database file '%s' could not be found.\n" % (dbFile,))
    sys.exit(1)

# If the cache file does not exist OR the database has been modified since the
# cache file has this has the side-effect of being able to touch the cache
# file to stop it from being updated. Good thing we have the -f option for any
# accidental touches (or if you copy the cache to a new location).
if not os.path.isfile(cacheFile) \
    or os.path.getmtime(dbFile) > os.path.getmtime(cacheFile) \
    or (os.path.isfile(stickerFile) and os.path.getmtime(stickerFile) > os.path.getmtime(cacheFile)) \
    or forceUpdate:
    if dataDir:
        print "Updating database cache..."

    # If the cache directory does not exist, create it. The dirname function
    # just removes the "/mpddb.cache" from the end.
    if not os.path.isdir(os.path.dirname(cacheFile)):
        os.mkdir(os.path.dirname(cacheFile))

    # Now, parse that database!
    tracks = parsedatabase(dbFile)
    if stickerFile:
        tracks = parseStickerDB(stickerFile, tracks)
    
    # Save the parsed stuff to the cache file and close the database file
    # handler. That's not strictly required, python will clean up when the
    # script ends, but you can't unmount volumes with file handlers pointing
    # to them, so it makes a mess.
    savegubbage(tracks, cacheFile)
else:
    # Oh, goodie, we don't need to go through all that arduous parsing as we
    # have a valid cache file :D
    if dataDir:
        print "Loading database cache..."
    # Open it for reading, load the stuff in the file into the tracks list,
    # close the file handler, and have a party.
    tracks = loadgubbage(cacheFile)
    try:
        if tracks:
            assert type(tracks) == type({})
            if len(tracks.keys()) > 1:
                assert isinstance(tracks.values()[-1], Track)
    except:
        raise Exception("Restoring from old cache won't work, please use -f.")

if dataDir:
    # add pre-existing playlists to our list
    for playlistfile in [os.path.join(dataDir, f) for f in os.listdir(dataDir)]:
        try:
            playlist = loadgubbage(playlistfile)
            assert isinstance(playlist, Playlist)
        except:
            raise Exception("Restoring old playlists won't work, please rm '%s'." % (playlistfile,))
        playlists.append(playlist)
        
# Now regenerate!
for playlist in playlists:
    playlist.findMatchingTracks(tracks)

    if not dataDir: # stdout
        for track in playlist.tracks:
            print track.file
    else: # write to .m3u & save
        playlist.writeM3u(playlistDir)
        playlist.save(dataDir)
