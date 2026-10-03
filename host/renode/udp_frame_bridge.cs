// Tunnels raw Ethernet frames between a Renode switch and a host process over
// 127.0.0.1 UDP, one datagram per frame. Renode has no DHCP server and on
// Windows no TAP without a driver; netpeer.py is the other end.
// $PROD/maps/sim-coverage/SIM-02-findings.md
using System;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure;
using Antmicro.Renode.Network;
using Antmicro.Renode.Peripherals.Network;

namespace Antmicro.Renode.Peripherals.Network
{
    public static class UdpFrameBridgeExtensions
    {
        public static void CreateUdpFrameBridge(this Emulation emulation, string name, int listenPort, int peerPort)
        {
            emulation.ExternalsManager.AddExternal(new UdpFrameBridge(listenPort, peerPort), name);
        }
    }

    public class UdpFrameBridge : IMACInterface, IExternal, IDisposable
    {
        public UdpFrameBridge(int listenPort, int peerPort)
        {
            peer = new IPEndPoint(IPAddress.Loopback, peerPort);
            sock = new UdpClient(new IPEndPoint(IPAddress.Loopback, listenPort));
            MAC = MACAddress.Parse("02:00:00:00:00:fe");
            new Thread(Loop) { IsBackground = true, Name = "UdpFrameBridge" }.Start();
        }

        public void ReceiveFrame(EthernetFrame frame)
        {
            var bytes = frame.Bytes;
            try { sock.Send(bytes, bytes.Length, peer); } catch(Exception) { }
        }

        public void Dispose()
        {
            running = false;
            sock.Close();
        }

        public event Action<EthernetFrame> FrameReady;
        public MACAddress MAC { get; set; }

        private void Loop()
        {
            var any = new IPEndPoint(IPAddress.Any, 0);
            while(running)
            {
                byte[] data;
                try { data = sock.Receive(ref any); } catch(Exception) { return; }
                if(EthernetFrame.TryCreateEthernetFrame(data, true, out var frame))
                {
                    FrameReady?.Invoke(frame);
                }
            }
        }

        private volatile bool running = true;
        private readonly UdpClient sock;
        private readonly IPEndPoint peer;
    }
}
