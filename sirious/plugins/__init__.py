from pydispatch import dispatcher

from sirious import SiriObjects


class SiriPlugin(object):
    proxy = None

    def respond(self, text, speakableText=None, dialogueIdentifier='Misc#ident', listenAfterSpeaking=False):
        self.proxy.blocking = True
        root = SiriObjects.AddViews()
        root.make_root(ref_id=self.proxy.ref_id)
        root.views.append(SiriObjects.Utterance(text=text, speakableText=speakableText, dialogueIdentifier=dialogueIdentifier, listenAfterSpeaking=listenAfterSpeaking))
        self.proxy.inject_plist(root.to_dict())

    def ask(self, handler, text, speakableText=None, dialogueIdentifier='Misc#ident', handler_kwargs={}):
        self.respond(text, speakableText, dialogueIdentifier, listenAfterSpeaking=True)
        self.proxy.blocking = True
        def handle_answer(*a, **kw):
            del(kw['sender'])
            del(kw['signal'])
            handler_kwargs.update(kw)
            handler(*a, **handler_kwargs)
            dispatcher.disconnect(handle_answer, signal='consume_phrase')
        dispatcher.connect(handle_answer, signal='consume_phrase')

    def complete(self):
        request_complete = SiriObjects.RequestCompleted()
        request_complete.make_root(self.proxy.ref_id)
        self.proxy.inject_plist(request_complete.to_dict())

    def plist_from_server(self, plist):
        return plist

    def plist_from_client(self, plist):
        return plist
