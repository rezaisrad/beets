# This file is part of beets.
# Copyright 2016, Adrian Sampson.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""Adds Discogs album search support to the autotagger. Requires the
python3-discogs-client library.
"""

import beets.ui
from beets import config
from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.plugins import MetadataSourcePlugin, BeetsPlugin, get_distance
import confuse
from discogs_client import Release, Master, Client
from discogs_client.exceptions import DiscogsAPIError
from requests.exceptions import ConnectionError
import requests
import http.client
import beets
import re
import time
import json
import socket
import os
import traceback
from string import ascii_lowercase


USER_AGENT = f'beets/{beets.__version__} +https://beets.io/'
API_KEY = 'rAzVUQYRaoFjeBjyWuWZ'
API_SECRET = 'plxtUTqoCzwxZpqdPysCwGuBSmZNdZVy'

# Exceptions that discogs_client should really handle but does not.
CONNECTION_ERRORS = (ConnectionError, socket.error, http.client.HTTPException,
                     ValueError,  # JSON decoding raises a ValueError.
                     DiscogsAPIError)


class DiscogsPlugin(BeetsPlugin):

    def __init__(self):
        super().__init__()
        self.config.add({
            'apikey': API_KEY,
            'apisecret': API_SECRET,
            'tokenfile': 'discogs_token.json',
            'source_weight': 0.5,
            'user_token': '',
            'separator': ', ',
            'index_tracks': False,
            'append_style_genre': False,
        })
        self.config['apikey'].redact = True
        self.config['apisecret'].redact = True
        self.config['user_token'].redact = True
        self.discogs_client = None
        self.register_listener('import_begin', self.setup)

    def setup(self, session=None):
        """Create the `discogs_client` field. Authenticate if necessary.
        """
        c_key = self.config['apikey'].as_str()
        c_secret = self.config['apisecret'].as_str()

        # Try using a configured user token (bypassing OAuth login).
        user_token = self.config['user_token'].as_str()
        if user_token:
            # The rate limit for authenticated users goes up to 60
            # requests per minute.
            self.discogs_client = Client(USER_AGENT, user_token=user_token)
            return

        # Get the OAuth token from a file or log in.
        try:
            with open(self._tokenfile()) as f:
                tokendata = json.load(f)
        except OSError:
            # No token yet. Generate one.
            token, secret = self.authenticate(c_key, c_secret)
        else:
            token = tokendata['token']
            secret = tokendata['secret']

        self.discogs_client = Client(USER_AGENT, c_key, c_secret,
                                     token, secret)

    def reset_auth(self):
        """Delete token file & redo the auth steps.
        """
        os.remove(self._tokenfile())
        self.setup()

    def _tokenfile(self):
        """Get the path to the JSON file for storing the OAuth token.
        """
        return self.config['tokenfile'].get(confuse.Filename(in_app_dir=True))

    def authenticate(self, c_key, c_secret):
        # Get the link for the OAuth page.
        auth_client = Client(USER_AGENT, c_key, c_secret)
        try:
            _, _, url = auth_client.get_authorize_url()
        except CONNECTION_ERRORS as e:
            self._log.debug('connection error: {0}', e)
            raise beets.ui.UserError('communication with Discogs failed')

        beets.ui.print_("To authenticate with Discogs, visit:")
        beets.ui.print_(url)

        # Ask for the code and validate it.
        code = beets.ui.input_("Enter the code:")
        try:
            token, secret = auth_client.get_access_token(code)
        except DiscogsAPIError:
            raise beets.ui.UserError('Discogs authorization failed')
        except CONNECTION_ERRORS as e:
            self._log.debug('connection error: {0}', e)
            raise beets.ui.UserError('Discogs token request failed')

        # Save the token for later use.
        self._log.debug('Discogs token {0}, secret {1}', token, secret)
        with open(self._tokenfile(), 'w') as f:
            json.dump({'token': token, 'secret': secret}, f)

        return token, secret

    def album_distance(self, items, album_info, mapping):
        """Returns the album distance.
        """
        return get_distance(
            data_source='Discogs',
            info=album_info,
            config=self.config
        )

    def track_distance(self, item, track_info):
        """Returns the track distance.
        """
        return get_distance(
            data_source='Discogs',
            info=track_info,
            config=self.config
        )

    def candidates(self, items, artist, album, va_likely, extra_tags=None):
        """Returns a list of AlbumInfo objects for discogs search results
        matching an album and artist (if not various).
        """
        if not self.discogs_client:
            return

        if not album and not artist:
            self._log.debug('Skipping Discogs query. Files missing album and '
                            'artist tags.')
            return []

        if va_likely:
            query = album
        else:
            query = f'{artist} {album}'
        try:
            return self.get_albums(query)
        except DiscogsAPIError as e:
            self._log.debug('API Error: {0} (query: {1})', e, query)
            if e.status_code == 401:
                self.reset_auth()
                return self.candidates(items, artist, album, va_likely)
            else:
                return []
        except CONNECTION_ERRORS:
            self._log.debug('Connection error in album search', exc_info=True)
            return []

    @staticmethod
    def extract_release_id_regex(album_id):
        """Returns the Discogs_id or None."""
        # Discogs-IDs are simple integers. In order to avoid confusion with
        # other metadata plugins, we only look for very specific formats of the
        # input string:
        # - plain integer, optionally wrapped in brackets and prefixed by an
        #   'r', as this is how discogs displays the release ID on its webpage.
        # - legacy url format: discogs.com/<name of release>/release/<id>
        # - current url format: discogs.com/release/<id>-<name of release>
        # See #291, #4080 and #4085 for the discussions leading up to these
        # patterns.
        # Regex has been tested here https://regex101.com/r/wyLdB4/2

        for pattern in [
                r'^\[?r?(?P<id>\d+)\]?$',
                r'discogs\.com/release/(?P<id>\d+)-',
                r'discogs\.com/[^/]+/release/(?P<id>\d+)',
        ]:
            match = re.search(pattern, album_id)
            if match:
                return int(match.group('id'))

        return None

    def album_for_id(self, album_id):
        """Fetches an album by its Discogs ID and returns an AlbumInfo object
        or None if the album is not found.
        """
        if not self.discogs_client:
            return

        self._log.debug('Searching for release {0}', album_id)

        discogs_id = self.extract_release_id_regex(album_id)

        if not discogs_id:
            return None

        result = Release(self.discogs_client, {'id': discogs_id})
        # Try to obtain title to verify that we indeed have a valid Release
        try:
            getattr(result, 'title')
        except DiscogsAPIError as e:
            if e.status_code != 404:
                self._log.debug('API Error: {0} (query: {1})', e,
                                result.data['resource_url'])
                if e.status_code == 401:
                    self.reset_auth()
                    return self.album_for_id(album_id)
            return None
        except CONNECTION_ERRORS:
            self._log.debug('Connection error in album lookup',
                            exc_info=True)
            return None
        return self.get_album_info(result)

    def get_albums(self, query):
        """Returns a list of AlbumInfo objects for a discogs search query.
        """
        # Strip non-word characters from query. Things like "!" and "-" can
        # cause a query to return no results, even if they match the artist or
        # album title. Use `re.UNICODE` flag to avoid stripping non-english
        # word characters.
        query = re.sub(r'(?!\.)(?u)\W+', ' ', query)
        # Strip medium information from query, Things like "CD1" and "disk 1"
        # can also negate an otherwise positive result.
        query = re.sub(r'(?i)\b(CD|disc)\s*\d+', '', query)

        try:
            releases = self.discogs_client.search(query).page(1)

        except CONNECTION_ERRORS:
            self._log.debug("Communication error while searching for {0!r}",
                            query, exc_info=True)
            return []

        return [album for album in map(self.get_album_info, releases[:5])
                if album]

    def get_master_year(self, master_id):
        """Fetches a master release given its Discogs ID and returns its year
        or None if the master release is not found.
        """
        self._log.debug('Searching for master release {0}', master_id)
        result = Master(self.discogs_client, {'id': master_id})

        try:
            year = result.fetch('year')
            return year
        except DiscogsAPIError as e:
            if e.status_code != 404:
                self._log.debug('API Error: {0} (query: {1})', e,
                                result.data['resource_url'])
                if e.status_code == 401:
                    self.reset_auth()
                    return self.get_master_year(master_id)
            return None
        except CONNECTION_ERRORS:
            self._log.debug('Connection error in master release lookup',
                            exc_info=True)
            return None

    def get_album_info(self, result):
        """Returns an AlbumInfo object for a discogs Release object.
        """
        # Explicitly reload the `Release` fields, as they might not be yet
        # present if the result is from a `discogs_client.search()`.
        # if not result.data.get('artists'):
        #     result.refresh()
        # print(result.data)
        if result.data['master_url'] is None:
            url_result = self.discogs_client._get(result.data['resource_url'])
        else:
            url_result = self.discogs_client._get(result.data['master_url'])

        # if it is not a main release, update to using the main release,
        # but only if the main release exists
        if 'main_release_url' in url_result.keys():
            main_release = requests.get(url_result['main_release_url']).json()
            if 'id' in main_release.keys():
                url_result = main_release

        # Sanity check for required fields. The list of required fields is
        # defined at Guideline 1.3.1.a, but in practice some releases might be
        # lacking some of these fields. This function expects at least:
        # `artists` (>0), `title`, `id`, `tracklist` (>0)
        # https://www.discogs.com/help/doc/submission-guidelines-general-rules
        if not all([url_result.get(k) for k in ['artists', 'title', 'id',
                                                 'tracklist']]):
            self._log.warning("Release does not contain the required fields")
            return None

        artist = url_result['artists'][0]['name']
        artist_id = url_result['artists'][0]['id']

        album = url_result['title']
        album_id = url_result['id']

        # Use `.data` to access the tracklist directly instead of the
        # convenient `.tracklist` property, which will strip out useful artist
        # information and leave us with skeleton `Artist` objects that will
        # each make an API call just to get the same data back.
        tracks = self.get_tracks(url_result['tracklist'])

        # Extract information for the optional AlbumInfo fields, if possible.
        va = url_result['artists'][0].get('name', '').lower() == 'various'
        year = url_result['year']
        mediums = [t.medium for t in tracks]
        country = result.data['country']
        data_url = url_result['uri']
        style = self.format(url_result['styles'])
        base_genre = self.format(url_result['genres'])

        if self.config['append_style_genre'] and style:
            genre = self.config['separator'].as_str().join([base_genre, style])
        else:
            genre = base_genre

        discogs_albumid = self.extract_release_id_regex(url_result['uri'])

        # Extract information for the optional AlbumInfo fields that are
        # contained on nested discogs fields.
        albumtype = media = label = catalogno = labelid = None
        if result.data.get('formats'):
            albumtype = ', '.join(
                result.data['formats'][0].get('descriptions', [])) or None
            media = result.data['formats'][0]['name']
        if result.data.get('labels'):
            label = result.data['labels'][0].get('name')
            catalogno = result.data['labels'][0].get('catno')
            labelid = result.data['labels'][0].get('id')

        # Additional cleanups (various artists name, catalog number, media).
        if va:
            artist = config['va_name'].as_str()
        if catalogno == 'none':
            catalogno = None
        # Explicitly set the `media` for the tracks, since it is expected by
        # `autotag.apply_metadata`, and set `medium_total`.
        for track in tracks:
            track.media = media
            track.medium_total = mediums.count(track.medium)
            # Discogs does not have track IDs. Invent our own IDs as proposed
            # in #2336.
            track.track_id = str(album_id) + "-" + track.track_alt

        # Retrieve master release id (returns None if there isn't one).
        master_id = result.data.get('master_id')
        # Assume `original_year` is equal to `year` for releases without
        # a master release, otherwise fetch the master release.
        original_year = self.get_master_year(master_id) if master_id else year

        return AlbumInfo(album=album, album_id=album_id, artist=artist,
                         artist_id=artist_id, tracks=tracks,
                         albumtype=albumtype, va=va, year=year,
                         label=label, mediums=len(set(mediums)),
                         releasegroup_id=master_id, catalognum=catalogno,
                         country=country, style=style, genre=genre,
                         media=media, original_year=original_year,
                         data_source='Discogs', data_url=data_url,
                         discogs_albumid=discogs_albumid,
                         discogs_labelid=labelid, discogs_artistid=artist_id)

    def format(self, classification):
        if classification:
            return self.config['separator'].as_str() \
                .join(sorted(classification))
        else:
            return None

    def get_tracks(self, tracklist):
        """Returns a list of TrackInfo objects for a discogs tracklist.
        """
        try:
            clean_tracklist = self.coalesce_tracks(tracklist)
        except Exception as exc:
            # FIXME: this is an extra precaution for making sure there are no
            # side effects after #2222. It should be removed after further
            # testing.
            self._log.debug('{}', traceback.format_exc())
            self._log.error('uncaught exception in coalesce_tracks: {}', exc)
            clean_tracklist = tracklist
        tracks = []
        index_tracks = {}
        index = 0
        # Distinct works and intra-work divisions, as defined by index tracks.
        divisions, next_divisions = [], []
        for track in clean_tracklist:
            # Only real tracks have `position`. Otherwise, it's an index track.
            if track['position']:
                index += 1
                if next_divisions:
                    # End of a block of index tracks: update the current
                    # divisions.
                    divisions += next_divisions
                    del next_divisions[:]
                track_info = self.get_track_info(track, index, divisions)
                track_info.track_alt = track['position']
                tracks.append(track_info)
            else:
                next_divisions.append(track['title'])
                # We expect new levels of division at the beginning of the
                # tracklist (and possibly elsewhere).
                try:
                    divisions.pop()
                except IndexError:
                    pass
                index_tracks[index + 1] = track['title']

        # Fix up medium and medium_index for each track. Discogs position is
        # unreliable, but tracks are in order.
        medium = None
        medium_count, index_count, side_count = 0, 0, 0
        sides_per_medium = 1

        # If a medium has two sides (ie. vinyl or cassette), each pair of
        # consecutive sides should belong to the same medium.
        if all([track.medium is not None for track in tracks]):
            m = sorted({track.medium.lower() for track in tracks})
            # If all track.medium are single consecutive letters, assume it is
            # a 2-sided medium.
            if ''.join(m) in ascii_lowercase:
                sides_per_medium = 2

        for track in tracks:
            # Handle special case where a different medium does not indicate a
            # new disc, when there is no medium_index and the ordinal of medium
            # is not sequential. For example, I, II, III, IV, V. Assume these
            # are the track index, not the medium.
            # side_count is the number of mediums or medium sides (in the case
            # of two-sided mediums) that were seen before.
            medium_is_index = track.medium and not track.medium_index and (
                len(track.medium) != 1 or
                # Not within standard incremental medium values (A, B, C, ...).
                ord(track.medium) - 64 != side_count + 1
            )

            if not medium_is_index and medium != track.medium:
                side_count += 1
                if sides_per_medium == 2:
                    if side_count % sides_per_medium:
                        # Two-sided medium changed. Reset index_count.
                        index_count = 0
                        medium_count += 1
                else:
                    # Medium changed. Reset index_count.
                    medium_count += 1
                    index_count = 0
                medium = track.medium

            index_count += 1
            medium_count = 1 if medium_count == 0 else medium_count
            track.medium, track.medium_index = medium_count, index_count

        # Get `disctitle` from Discogs index tracks. Assume that an index track
        # before the first track of each medium is a disc title.
        for track in tracks:
            if track.medium_index == 1:
                if track.index in index_tracks:
                    disctitle = index_tracks[track.index]
                else:
                    disctitle = None
            track.disctitle = disctitle

        return tracks

    def coalesce_tracks(self, raw_tracklist):
        """Pre-process a tracklist, merging subtracks into a single track. The
        title for the merged track is the one from the previous index track,
        if present; otherwise it is a combination of the subtracks titles.
        """
        def add_merged_subtracks(tracklist, subtracks):
            """Modify `tracklist` in place, merging a list of `subtracks` into
            a single track into `tracklist`."""
            # Calculate position based on first subtrack, without subindex.
            idx, medium_idx, sub_idx = \
                self.get_track_index(subtracks[0]['position'])
            position = '{}{}'.format(idx or '', medium_idx or '')

            if tracklist and not tracklist[-1]['position']:
                # Assume the previous index track contains the track title.
                if sub_idx:
                    # "Convert" the track title to a real track, discarding the
                    # subtracks assuming they are logical divisions of a
                    # physical track (12.2.9 Subtracks).
                    tracklist[-1]['position'] = position
                else:
                    # Promote the subtracks to real tracks, discarding the
                    # index track, assuming the subtracks are physical tracks.
                    index_track = tracklist.pop()
                    # Fix artists when they are specified on the index track.
                    if index_track.get('artists'):
                        for subtrack in subtracks:
                            if not subtrack.get('artists'):
                                subtrack['artists'] = index_track['artists']
                    # Concatenate index with track title when index_tracks
                    # option is set
                    if self.config['index_tracks']:
                        for subtrack in subtracks:
                            subtrack['title'] = '{}: {}'.format(
                                index_track['title'], subtrack['title'])
                    tracklist.extend(subtracks)
            else:
                # Merge the subtracks, pick a title, and append the new track.
                track = subtracks[0].copy()
                track['title'] = ' / '.join([t['title'] for t in subtracks])
                tracklist.append(track)

        # Pre-process the tracklist, trying to identify subtracks.
        subtracks = []
        tracklist = []
        prev_subindex = ''
        for track in raw_tracklist:
            # Regular subtrack (track with subindex).
            if track['position']:
                _, _, subindex = self.get_track_index(track['position'])
                if subindex:
                    if subindex.rjust(len(raw_tracklist)) > prev_subindex:
                        # Subtrack still part of the current main track.
                        subtracks.append(track)
                    else:
                        # Subtrack part of a new group (..., 1.3, *2.1*, ...).
                        add_merged_subtracks(tracklist, subtracks)
                        subtracks = [track]
                    prev_subindex = subindex.rjust(len(raw_tracklist))
                    continue

            # Index track with nested sub_tracks.
            if not track['position'] and 'sub_tracks' in track:
                # Append the index track, assuming it contains the track title.
                tracklist.append(track)
                add_merged_subtracks(tracklist, track['sub_tracks'])
                continue

            # Regular track or index track without nested sub_tracks.
            if subtracks:
                add_merged_subtracks(tracklist, subtracks)
                subtracks = []
                prev_subindex = ''
            tracklist.append(track)

        # Merge and add the remaining subtracks, if any.
        if subtracks:
            add_merged_subtracks(tracklist, subtracks)

        return tracklist

    def get_track_info(self, track, index, divisions):
        """Returns a TrackInfo object for a discogs track.
        """
        title = track['title']
        if self.config['index_tracks']:
            prefix = ', '.join(divisions)
            if prefix:
                title = f'{prefix}: {title}'
        track_id = None
        medium, medium_index, _ = self.get_track_index(track['position'])
        artist, artist_id = MetadataSourcePlugin.get_artist(
            track.get('artists', [])
        )
        length = self.get_track_length(track['duration'])
        return TrackInfo(title=title, track_id=track_id, artist=artist,
                         artist_id=artist_id, length=length, index=index,
                         medium=medium, medium_index=medium_index)

    def get_track_index(self, position):
        """Returns the medium, medium index and subtrack index for a discogs
        track position."""
        # Match the standard Discogs positions (12.2.9), which can have several
        # forms (1, 1-1, A1, A1.1, A1a, ...).
        match = re.match(
            r'^(.*?)'           # medium: everything before medium_index.
            r'(\d*?)'           # medium_index: a number at the end of
                                # `position`, except if followed by a subtrack
                                # index.
                                # subtrack_index: can only be matched if medium
                                # or medium_index have been matched, and can be
            r'((?<=\w)\.[\w]+'  # - a dot followed by a string (A.1, 2.A)
            r'|(?<=\d)[A-Z]+'   # - a string that follows a number (1A, B2a)
            r')?'
            r'$',
            position.upper()
        )

        if match:
            medium, index, subindex = match.groups()

            if subindex and subindex.startswith('.'):
                subindex = subindex[1:]
        else:
            self._log.debug('Invalid position: {0}', position)
            medium = index = subindex = None
        return medium or None, index or None, subindex or None

    def get_track_length(self, duration):
        """Returns the track length in seconds for a discogs duration.
        """
        try:
            length = time.strptime(duration, '%M:%S')
        except ValueError:
            return None
        return length.tm_min * 60 + length.tm_sec
