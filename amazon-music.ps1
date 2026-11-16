$amazonPort = 0
$amazonSocket = $null
$amazonProcessId = 0
$amazonAppId = ''
$amazonRetryAt = [DateTime]::MinValue
$amazonConnectionError = ''

function Start-AmazonMusic {
    $package = Get-AppxPackage -Name AmazonMobileLLC.AmazonMusic | Select-Object -First 1
    if (!$package) { throw 'The Microsoft Store version of Amazon Music is required.' }
    $script:amazonAppId = "$($package.PackageFamilyName)!AmazonMobileLLC.AmazonMusic"
    if (!('NpbAmazonActivation' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("2e941141-7f97-4756-ba1d-9decde894a3d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface INpbAmazonActivation {
    [PreserveSig]
    int ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string id,
        [MarshalAs(UnmanagedType.LPWStr)] string arguments, uint options, out uint pid);
}
public static class NpbAmazonActivation {
    public static uint Launch(string id, string arguments) {
        var cls = Type.GetTypeFromCLSID(new Guid("45ba127d-10a8-46ea-8ab7-56ea9078943c"));
        var app = (INpbAmazonActivation)Activator.CreateInstance(cls);
        try {
            uint pid;
            Marshal.ThrowExceptionForHR(app.ActivateApplication(id, arguments, 0, out pid));
            return pid;
        } finally {
            Marshal.ReleaseComObject(app);
        }
    }
}
'@
    }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { $port = $listener.LocalEndpoint.Port } finally { $listener.Stop() }
    $directory = $package.InstallLocation.TrimEnd('\') + '\'
    $running = Get-CimInstance Win32_Process -Filter "Name='Amazon Music.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($directory, [StringComparison]::OrdinalIgnoreCase) -and $_.CommandLine -notmatch '--type=' }
    foreach ($entry in $running) {
        $process = Get-Process -Id $entry.ProcessId -ErrorAction SilentlyContinue
        if ($process -and (!$process.CloseMainWindow() -or !$process.WaitForExit(750))) {
            Stop-Process -InputObject $process -ErrorAction SilentlyContinue
            if (!$process.WaitForExit(1200)) { throw 'Could not close Amazon Music.' }
        }
    }
    try {
        $script:amazonProcessId = [NpbAmazonActivation]::Launch($amazonAppId, "--remote-debugging-port=$port --remote-debugging-address=127.0.0.1")
        $script:amazonPort = $port
        $script:amazonRetryAt = [DateTime]::MinValue
        $script:amazonConnectionError = ''
    } catch {
        $null = [NpbAmazonActivation]::Launch($amazonAppId, '')
        throw
    }
}

function Stop-AmazonMusicConnection {
    if ($amazonSocket) { $amazonSocket.Dispose(); $script:amazonSocket = $null }
    if (!$amazonPort) { return }
    $process = Get-Process -Id $amazonProcessId -ErrorAction SilentlyContinue
    $script:amazonPort = 0
    $script:amazonProcessId = 0
    if (!$process -or $process.ProcessName -ne 'Amazon Music') { return }
    if (!$process.CloseMainWindow() -or !$process.WaitForExit(750)) {
        Stop-Process -InputObject $process -ErrorAction SilentlyContinue
        if (!$process.WaitForExit(1200)) { throw 'Close Amazon Music to end its debugging session.' }
    }
    $null = [NpbAmazonActivation]::Launch($amazonAppId, '')
}

function Get-AmazonMusicSample($trackKey, $target, $cachedArtwork) {
    $timeout = $null
    try {
        if (!$amazonSocket -or $amazonSocket.State -ne 'Open') {
            if ([DateTime]::UtcNow -lt $amazonRetryAt) { return }
            $script:amazonRetryAt = [DateTime]::UtcNow.AddSeconds(2)
            if ($amazonSocket) { $amazonSocket.Dispose(); $script:amazonSocket = $null }
            $listeners = @(Get-NetTCPConnection -LocalPort $amazonPort -State Listen -ErrorAction SilentlyContinue)
            if (!$listeners -or @($listeners | Where-Object {
                $_.OwningProcess -ne $amazonProcessId -or $_.LocalAddress -notin @('127.0.0.1', '::1')
            }).Count) { return }
            $pages = Invoke-RestMethod "http://127.0.0.1:$amazonPort/json/list" -TimeoutSec 1
            $page = @($pages) | Where-Object { $_.type -eq 'page' -and $_.title -eq 'Amazon Music Desktop' } | Select-Object -First 1
            if (!$page -or $page.webSocketDebuggerUrl -notmatch "^ws://127\.0\.0\.1:${amazonPort}/devtools/page/[a-zA-Z0-9-]+$") { return }
            $timeout = [Threading.CancellationTokenSource]::new(2000)
            $script:amazonSocket = [Net.WebSockets.ClientWebSocket]::new()
            $amazonSocket.ConnectAsync([uri]$page.webSocketDebuggerUrl, $timeout.Token).GetAwaiter().GetResult() | Out-Null
        }
        if (!$timeout) { $timeout = [Threading.CancellationTokenSource]::new(1500) }
        $request = ConvertTo-Json -Depth 4 -Compress -InputObject @{ key = $trackKey; target = $target; cachedArtwork = $cachedArtwork }
        $expression = @'
(seek => {
    const root = [...document.querySelectorAll('*')].find(node => node.__vue__ && node.__vue__.$store);
    if (!root) return null;
    const player = root.__vue__.$store.state.player;
    const model = player && player.model;
    if (!model || !player.progress) return null;
    const playable = model.currentPlayable;
    const track = playable && playable.track;
    if (!track || !(model.duration > 0) || !Number.isFinite(player.progress.currentTime)) return null;
    const slider = document.querySelector('.slider.progressBar');
    const key = String(playable.id || track.uniqueId || track.asin || track.title);
    const seekable = Boolean(slider && slider.__vue__ && slider.__vue__.$listeners && slider.__vue__.$listeners.dragFinished);
    const seekAccepted = seekable && seek.key === key && Number.isFinite(seek.target);
    if (seekAccepted) {
        slider.__vue__.$emit('dragFinished', Math.max(0, Math.min(100, seek.target * 100000 / model.duration)));
    }
    const artworkUrl = (track.album || {}).image || '';
    let artwork = '';
    if (typeof artworkUrl === 'string' && artworkUrl && !(seek.cachedArtwork &&
            seek.cachedArtwork.key === key && seek.cachedArtwork.url === artworkUrl)) {
        try {
            const url = new URL(artworkUrl, document.baseURI).href;
            const image = [...document.images].find(node => node.complete && node.naturalWidth &&
                (node.src === url || node.currentSrc === url));
            if (image) {
                const canvas = document.createElement('canvas');
                const scale = Math.min(1, 192 / Math.max(image.naturalWidth, image.naturalHeight));
                canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
                canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
                canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
                artwork = canvas.toDataURL('image/jpeg', 0.86).slice('data:image/jpeg;base64,'.length);
            }
        } catch (_) { /* The image is not readable from this renderer. */ }
    }
    return {
        key, title: track.title || '', credit: (track.artist || {}).name || '',
        duration: model.duration / 1000, position: player.progress.currentTime / 1000,
        playing: model.state === 'PLAYING', seekable, seekAccepted,
        artworkUrl, artwork
    };
})
'@
        $payload = @{ id = 1; method = 'Runtime.evaluate'; params = @{ expression = "$expression($request)"; returnByValue = $true } }
        $bytes = [Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Depth 5 -Compress))
        $amazonSocket.SendAsync([ArraySegment[byte]]::new($bytes), [Net.WebSockets.WebSocketMessageType]::Text, $true, $timeout.Token).GetAwaiter().GetResult() | Out-Null
        $buffer = [byte[]]::new(16384)
        do {
            $output = [IO.MemoryStream]::new()
            try {
                do {
                    $part = $amazonSocket.ReceiveAsync([ArraySegment[byte]]::new($buffer), $timeout.Token).GetAwaiter().GetResult()
                    if ($part.MessageType -ne 'Text' -or $output.Length + $part.Count -gt 65536) { throw 'Invalid Amazon Music response.' }
                    $output.Write($buffer, 0, $part.Count)
                } until ($part.EndOfMessage)
                $response = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString($output.ToArray()))
            } finally { $output.Dispose() }
        } until ($response.id -eq 1)
        if ($response.error -or $response.result.exceptionDetails) { throw 'Amazon Music playback data is unavailable.' }
        $script:amazonConnectionError = ''
        return $response.result.result.value
    } catch {
        $script:amazonConnectionError = $_.Exception.GetBaseException().Message
        if ($amazonSocket) { $amazonSocket.Dispose(); $script:amazonSocket = $null }
    } finally { if ($timeout) { $timeout.Dispose() } }
}
