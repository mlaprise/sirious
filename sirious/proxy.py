from binascii import hexlify, unhexlify
import re
import sys
import zlib

from biplist import readPlistFromString, writePlistToString
from pydispatch import dispatcher
from twisted.internet import protocol, reactor, ssl
from twisted.protocols.basic import LineReceiver
from twisted.protocols.portforward import ProxyClientFactory


class SiriProxy(LineReceiver):
    """ Base class for the SiriProxy - performs the majority of the siri protocol and sirious plugin handling. """
    peer = None ## the other end! (self.peer.peer == self)
    blocking = False
    ref_id = None ## last refId seen

    def __init__(self, plugins=[], triggers=[]):
        self.zlib_d = zlib.decompressobj()
        self.zlib_c = zlib.compressobj()
        self.plugins = plugins ## registered plugins
        self.triggers = triggers ## two-tuple mapping regex->plugin_function

    def setPeer(self, peer):
        self.peer = peer

    def lineReceived(self, line):
        """ Handles simple HTML-style headers
            @todo parse X-Ace-Host: header
        """
        direction = '>' if self.__class__ == SiriProxyServer else '<'
        print direction, line
        self.peer.sendLine(line)
        if not line:
            self.setRawMode()

    def rawDataReceived(self, data):
        """
            This is where the main Siri protocol handling is done.
            Raw data consists of:
                (aaccee02)?<zlib_packed_data>
            Once decompressed the data takes the form:
                (header)(body)
            The header is a binary hex representation of is one of three things:
                0200000000
                0300000000
                0400000000
            Where:
                02... indicated a binary plist payload (followed by the payload size)
                03... indicates a iphone->server ping (followed by the sequence id)
                04... indicates a server->iphone pong (followed by the sequence id)
            And the trailing digits are provided in base 16.
            The body is a binary plist.

            The aaccee02 header is immediately forwarded, as are ping/pong packets.

            04... packets are parsed and passed through `process_plist` before
            being re-injected (or discarded).
        """
        if self.zlib_d.unconsumed_tail:
            data = self.zlib_d.unconsumed_tail + data
        if hexlify(data[0:4]) == 'aaccee02':
            self.peer.transport.write(data[0:4])
            data = data[4:]
        ## Add `data` to decompress stream
        udata = self.zlib_d.decompress(data)
        if udata:
            ## If we get decompressed output, process it
            header = hexlify(udata[0:5])
            if header[1] in [3, 4]:
                ## Ping/Pong packets - pass them straight through
                return self.peer.transport.write(data)
            size = int(header[2:], 16)
            body = udata[5:(size + 5)]
            if body:
                ## Parse the plist data
                plist = readPlistFromString(body)
                ## and have the server/client process it
                direction = '>' if self.__class__ == SiriProxyServer else '<'
                print direction, plist['class'], plist.get('refId', '')
                plist = self.process_plist(plist)
                if plist:
                    block = False
                    ## Stop blocking if it's a new session
                    if self.blocking and self.ref_id != plist['refId']:
                        self.blocking = False
                    if isinstance(self.blocking, bool) and self.blocking:
                        block = True
                    if isinstance(self.blocking, int) and self.blocking > 0:
                        self.blocking -= 1
                        block = True
                    if not block:
                        self.inject_plist(plist)
                    else:
                        print "!", plist['class'], self.blocking
                    return plist
                else:
                    print "!", plist['class'], plist.get('refId', '')

    def process_plist(self, plist):
        """ Primarily used for logging and to call the appropriate client/server methods. """
        #from pprint import pprint
        #pprint(plist)
        #print
        ## Offer plugins a chance to intercept/modify plists early on
        for plugin in self.plugins:
            plugin.proxy = self
            if self.__class__ == SiriProxyServer:
                plist = plugin.plist_from_client(plist)
            if self.__class__ == SiriProxyClient:
                plist = plugin.plist_from_server(plist)
            ## If a plugin returns None, the plist has been blocked
            if not plist:
                return
        return plist

    def inject_plist(self, plist):
        """
            Inject a plist into the session.
            This is essentially a reverse of `rawDataReceived`:
                * the plist dictionary is converted into to a binary plist
                * the size is measured and the appropriate 02... header generated
                * header and body are concatenated, compressed, and injected.
        """
        ref_id = plist.get('refId', None)
        if ref_id:
            self.ref_id = ref_id
        data = writePlistToString(plist)
        data_len = len(data)
        if data_len > 0:
            ## Add data_len to 0x200000000 and convert to hex, zero-padded to 10 digits
            header = '{:x}'.format(0x0200000000 + data_len).rjust(10, '0')
            data = self.zlib_c.compress(unhexlify(header) + data)
            self.peer.transport.write(data)
            self.peer.transport.write(self.zlib_c.flush(zlib.Z_FULL_FLUSH))

    def connectionLost(self, reason):
        """ Reset ref_id and disconnect peer """
        self.ref_id = None
        if self.peer:
            self.peer.transport.loseConnection()
            self.setPeer(None)


class SiriProxyClient(SiriProxy):
    def connectionMade(self):
        self.peer.setPeer(self)
        self.peer.transport.resumeProducing()

    def rawDataReceived(self, data):
        plist = SiriProxy.rawDataReceived(self, data)
        if plist:
            self.process_speech(plist)

    def process_speech(self, plist):
        phrase = None
        if plist['class'] == 'AddViews':
            phrase = ''
            if plist['properties']['views'][0]['properties']['dialogIdentifier'] == 'Common#unknownIntent':
                phrase = plist['properties']['views'][1]['properties']['commands'][0]['properties']['commands'][0]['properties']['utterance'].split('^')[3]
        if plist['class'] == 'SpeechRecognized':
            phrase = ''
            for phrase_plist in plist['properties']['recognition']['properties']['phrases']:
                for token in phrase_plist['properties']['interpretations'][0]['properties']['tokens']:
                    if token['properties']['removeSpaceBefore']:
                        phrase = phrase[:-1]
                    phrase += token['properties']['text']
                    if not token['properties']['removeSpaceAfter']:
                        phrase += ' '
        if phrase:
            print '[Speech Recognised (%s)] "%s"' % (plist['class'], phrase)
            try:
                dispatcher.getAllReceivers(signal='consume_phrase').next()
                dispatcher.send('consume_phrase', phrase=phrase, plist=plist)
            except StopIteration:
                for trigger, function in self.triggers:
                    if trigger.search(phrase):
                        function(phrase, plist)


class SiriProxyClientFactory(ProxyClientFactory):
    protocol = SiriProxyClient


class SiriProxyServer(SiriProxy):
    clientProtocolFactory = SiriProxyClientFactory

    def connectionMade(self):
        self.transport.pauseProducing()
        client = self.clientProtocolFactory()
        client.setServer(self)
        client.plugins = self.plugins
        client.triggers = self.triggers
        reactor.connectSSL(self.factory.host, self.factory.port, client, ssl.DefaultOpenSSLContextFactory(
            'keys/server.key', 'keys/server.crt'))

    def rawDataReceived(self, data):
        SiriProxy.rawDataReceived(self, data) ## returning a value seems to upset Twisted


class SiriProxyFactory(protocol.Factory):
    protocol = SiriProxyServer

    def __init__(self, plugins):
        self.host = '17.174.4.4'
        self.port = 443
        self.plugins = []
        for mod_name, cls_name, kwargs in plugins:
            __import__(mod_name)
            mod = sys.modules[mod_name]
            self.plugins.append((getattr(mod, cls_name), kwargs))

    def _get_plugin_triggers(self, instance):
        for attr_name in dir(instance):
            attr = getattr(instance, attr_name)
            if callable(attr) and hasattr(attr, 'triggers'):
                yield attr

    def buildProtocol(self, addr):
        protocol = self.protocol()
        for cls, plugin_kwargs in self.plugins:
            instance = cls(**plugin_kwargs)
            protocol.plugins.append(instance)
            for function in self._get_plugin_triggers(instance):
                for trigger in function.triggers:
                    trigger_re = re.compile(trigger, re.I)
                    protocol.triggers.append((trigger_re, function))
        protocol.factory = self
        return protocol
