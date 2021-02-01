from pyscipopt import Eventhdlr, SCIP_EVENTTYPE


class DebugEvents(Eventhdlr):

    def eventinit(self):
        print('LPSOLVED eventinit')
        self.model.catchEvent(SCIP_EVENTTYPE.LPSOLVED, self)
        self.model.catchEvent(SCIP_EVENTTYPE.ROWADDEDSEPA, self)
        self.model.catchEvent(SCIP_EVENTTYPE.ROWADDEDLP, self)
        self.model.catchEvent(SCIP_EVENTTYPE.NODEBRANCHED, self)
        self.model.catchEvent(SCIP_EVENTTYPE.UBTIGHTENED, self)

    def eventexit(self):
        print('LPSOLVED eventexit')
        self.model.dropEvent(SCIP_EVENTTYPE.LPSOLVED, self)
        self.model.dropEvent(SCIP_EVENTTYPE.ROWADDEDSEPA, self)
        self.model.dropEvent(SCIP_EVENTTYPE.ROWADDEDLP, self)
        self.model.dropEvent(SCIP_EVENTTYPE.NODEBRANCHED, self)
        self.model.dropEvent(SCIP_EVENTTYPE.UBTIGHTENED, self)

    def eventexec(self, event):
        if event.getType() == SCIP_EVENTTYPE.LPSOLVED:
            print('event - LPSOLVED')
        elif event.getType() == SCIP_EVENTTYPE.ROWADDEDSEPA:
            print('event - ROWADDEDSEPA')
        elif event.getType() == SCIP_EVENTTYPE.ROWADDEDLP:
            print('event - ROWADDEDLP')
        elif event.getType() == SCIP_EVENTTYPE.NODEBRANCHED:
            print('event - NODEBRANCHED')
        elif event.getType() == SCIP_EVENTTYPE.UBTIGHTENED:
            print('event - UBTIGHTENED')
        else:
            raise ValueError('event error')

# eventhdlr = MyEvent()
# model.includeEventhdlr(eventhdlr, "TestFirstLPevent", "python event handler to catch FIRSTLPEVENT")
