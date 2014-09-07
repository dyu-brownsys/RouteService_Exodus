import sys
sys.path.append('./gen-py')

from route import RouteService
from route.ttypes import *

from thrift.transport import TSocket
from thrift.transport import TTransport
from thrift.protocol import TBinaryProtocol
from thrift.server import TServer

import socket
from netaddr import *
import pprint

class RouteServiceHandler:
    def __init__(self):
        '''
        linkState describes the topology of this network:
        (srcswid, dstswid) -> (srcport, dstport, cost)
        '''
        self.linkState = {}
        '''
        routerList describes all routers' information:
        (swid) -> (port -> subnet)
        '''
        self.switchList = {}
        '''
        all path generated by floyd_warshall algorithm
        (src, dst) -> list(hops)
        '''
        self.routePath = {}
        self.routeTable = []
        self.log = {}

        # may be static routes in the config that need inclusion in the routing table
        self.static_routes = []

    def notifyMe(self, notify):
        print "Notification Received!"
        print notify
        '''
        Update LinkState Table
        '''
        if notify.notificationType.lower().startswith("linkstate"):
            if (len(notify.values) != 5 and notify.notificationType.lower() == "linkstate_up"):
                print "Invalid Notification!"
                return
            if (len(notify.values) != 4 and notify.notificationType.lower() == "linkstate_down"):
                print "Invalid Notification!"
                return

            srcsw = notify.values['srcsw']
            dstsw = notify.values['dstsw']
            srcpt = notify.values['srcpt']
            dstpt = notify.values['dstpt']
            if notify.notificationType.lower() == "linkstate_up":
                cost = int(notify.values['cost'])
            else:
                cost = None

            # avoid self loop
            if srcsw == dstsw:
                return

            if notify.notificationType.lower() == "linkstate_down":
                if (srcsw, dstsw) in self.linkState.keys():
                    del self.linkState[(srcsw, dstsw)]
                return

            elif notify.notificationType.lower() == "linkstate_up":
                # Two switch connect with two different cables, pick the less cost one
                if (srcsw, dstsw) in self.linkState.keys():
                    (oldSrcPt, oldDstPt, oldCost) = self.linkState[(srcsw,dstsw)]
                    if oldCost > cost:
                        self.linkState[(srcsw,dstsw)] = (srcpt, dstpt, cost)
                else:
                        self.linkState[(srcsw,dstsw)] = (srcpt, dstpt, cost)

        '''
        Update switchList
        '''
        if notify.notificationType.lower() == "switch_config":
            if len(notify.values) != 4:
                print "Invalid Notification!"
                return

            swid = notify.values['swid']
            ptid = notify.values['ptid']
            prefix = notify.values['prefix']
            mask = notify.values['mask']


            if swid not in self.switchList.keys():
                self.switchList[swid] = {}

            self.switchList[swid][ptid] = (prefix, mask)

        # This server needs to get routing (L3) port numbers on router, not physical ports on vlan
        # (if it's a virtual interface, might have to go out multiple physical ports)
        if notify.notificationType.lower() == "static_route":
            if len(notify.values) != 4:
                print "Invalid Notification!"
                return
            swid = notify.values['swid']
            outport = notify.values['outport']
            prefix = notify.values['prefix']
            mask = notify.values['mask']
            self.static_routes.append([swid,outport,prefix,mask])


        '''
        # Test Use
        print "************************"
        self.printLinkState()
        print "---------------------"
        self.printSwitchConfig()
        '''

    def doQuery(self, req):
        self.initMatrix()
        self.floyd_warshall()
        self.generateAllPath()
        self.generateRouteTable()
        print "*********"
        self.printRouteTable()

        print "Query Request Received!"
        print req
        if len(req.arguments) != 4:
            reply = QueryReply()
            reply.result = None
            reply.exception_code = "1"
            reply.exception_message = "Not Enough Arguments"
            return reply

        token = req.arguments

        req_swid = token[0]
        req_addr = token[1]
        req_mask = token[2]
        req_outport = token[3]

        reply = QueryReply()

        rt_result = filter(lambda (a,b,c,d): \
                    (isVal(req_swid) or a == req_swid) and \
                    (isVal(req_addr) or b == req_addr) and \
                    (isVal(req_mask) or c == req_mask) and \
                    (isVal(req_outport) or d == req_outport),
                    self.routeTable)

        result = []

        for a,b,c,d in rt_result:
            temp = [a,b,c,d]
            result.append(temp)

        result.extend(self.static_routes)
        reply.result = result

        return reply


    def generateRouteTable(self):
        # clear out the cached route table before regenerating
        self.routeTable = []
        result = {}
        for i in self.switchList.keys():
            for j in self.switchList.keys():
                if i == j:
                    #Add route to directly connect
                    #all ports
                    for port, subnet in self.switchList[i].iteritems():
                        prefix, mask = subnet
                        ip = IPNetwork(prefix + '/' + mask)
                        if (i, str(ip.network), mask) not in result.keys():
                            result[(i, str(ip.network), mask)] = port
                        #result.add((i, str(ip.network), mask, port))
                else:
                    for port, subnet in self.switchList[j].iteritems():
                        '''
                        #If j directly connect with i, ignore the attached port, because it has already added into the table
                        if (i,j) in self.linkState.keys():
                            srcpt, dstpt, cost = self.linkState[(i,j)]
                            if dstpt == port:
                                continue
                        '''
                        prefix, mask = subnet
                        ip = IPNetwork(prefix + '/' + mask)
                        if len(self.routePath[i,j]) == 0:
                            continue
                        nexthop = self.routePath[i,j][0]
                        srcpt, dstpt, cost = self.linkState[(i,nexthop)]
                        if (i, str(ip.network), mask) not in result.keys():
                            result[(i, str(ip.network), mask)] = srcpt

                        #result.add((i, str(ip.network), mask, srcpt))
        for key, value in result.iteritems():
            a,b,c = key
            self.routeTable.append((a, b, c, value))


        #self.routeTable = list(result)


    def initMatrix(self):
        #inital dist matrix with all infinity
        self.dist = {}
        for i in self.switchList.keys():
            self.dist[i] = {}
            for j in self.switchList.keys():
                self.dist[i][j] = float("inf")

        #initial next matrixt with all none
        self.nexthop = {}
        for i in self.switchList.keys():
            self.nexthop[i] = {}
            for j in self.switchList.keys():
                self.nexthop[i][j] = None

        #Fill the dist matrix and next matrix with value
        for key, item in self.linkState.iteritems():
            srcsw, dstsw = key
            srcpt, dstpt, cost = item
            self.dist[srcsw][dstsw] = cost
            self.nexthop[srcsw][dstsw] = dstsw

    def floyd_warshall(self):
        for k in self.switchList.keys():
            for i in self.switchList.keys():
                for j in self.switchList.keys():
                    if self.dist[i][k] + self.dist[k][j] < self.dist[i][j]:
                        self.dist[i][j] = self.dist[i][k] + self.dist[k][j]
                        self.nexthop[i][j] = self.nexthop[i][k]

    def path(self, src, dst):
        result = []
        if self.nexthop[src][dst] is None:
            return result
        while src != dst:
            src = self.nexthop[src][dst]
            result.append(src)
        return result

    def generateAllPath(self):
        for i in self.switchList.keys():
            for j in self.switchList.keys():
                #Ignore all path from i to i
                if i == j:
                    continue
                self.routePath[i,j] = self.path(i,j)

    def printLinkState(self):
        print self.linkState

    def printSwitchConfig(self):
        print self.switchList

    def printAllPath(self):
        for key, item in self.routePath.iteritems():
            print "%s : %s" % key, item

    def printRouteTable(self):
        for i in self.routeTable:
            print i

def isVal(a):
    b = a.replace(".","")
    return not b.isdigit()

handler = RouteServiceHandler()
processor = RouteService.Processor(handler)
transport = TSocket.TServerSocket("127.0.0.1", port = 9999)
tfactory = TTransport.TBufferedTransportFactory()
pfactory = TBinaryProtocol.TBinaryProtocolFactory()

server = TServer.TSimpleServer(processor, transport, tfactory, pfactory)

print "Starting thrift server in python..."
server.serve()
print "done!"
