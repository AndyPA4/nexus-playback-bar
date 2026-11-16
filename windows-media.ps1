param([string]$VideoPath)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$managerType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime]
$mediaType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties,Windows.Media.Control,ContentType=WindowsRuntime]
$streamType = [Windows.Storage.Streams.IRandomAccessStreamWithContentType,Windows.Storage.Streams,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.DataReader,Windows.Storage.Streams,ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Length -eq 1
} | Select-Object -First 1

function Wait-WinRT($operation, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    if (-not $task.Wait(1800)) {
        throw 'Windows media operation timed out.'
    }
    $task.GetAwaiter().GetResult()
}

function Read-ImageStream($stream) {
    try {
        if ($stream.Size -lt 1 -or $stream.Size -gt 2000000) { return '' }
        $reader = [Windows.Storage.Streams.DataReader]::new($stream)
        try {
            $length = Wait-WinRT ($reader.LoadAsync([uint32]$stream.Size)) ([uint32])
            if ($length -ne $stream.Size) { return '' }
            $bytes = [byte[]]::new([int]$length)
            $reader.ReadBytes($bytes)
            return [Convert]::ToBase64String($bytes)
        } finally {
            $reader.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

if ($VideoPath) {
    $fileType = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
    $thumbnailType = [Windows.Storage.FileProperties.StorageItemThumbnail,Windows.Storage,ContentType=WindowsRuntime]
    $mode = [Windows.Storage.FileProperties.ThumbnailMode,Windows.Storage,ContentType=WindowsRuntime]::VideosView
    $file = Wait-WinRT ($fileType::GetFileFromPathAsync($VideoPath)) $fileType
    $thumbnail = Wait-WinRT ($file.GetThumbnailAsync($mode, [uint32]640)) $thumbnailType
    if ($null -ne $thumbnail -and -not $thumbnail.IsImage) {
        $thumbnail.Dispose()
        exit
    }
    if ($null -ne $thumbnail) {
        [Console]::WriteLine((Read-ImageStream $thumbnail))
    }
    exit
}

function Test-BrowserSession([string]$app) {
    return $app -match '(^|[.\\])chrome([.!\\-]|$)|chromium|msedge|firefox|brave|vivaldi|opera'
}

function Test-Seekable($playback, $timeline) {
    return ($playback.Controls.IsPlaybackPositionEnabled -and
            $timeline.EndTime -gt $timeline.StartTime -and
            $timeline.MaxSeekTime -gt $timeline.MinSeekTime)
}

function Get-MediaSessionSummary($manager) {
    foreach ($session in $manager.GetSessions()) {
        try {
            $app = $session.SourceAppUserModelId
            $playback = $session.GetPlaybackInfo()
            $timeline = $session.GetTimelineProperties()
            @{
                app = $app
                status = [string]$playback.PlaybackStatus
                duration = ($timeline.EndTime - $timeline.StartTime).TotalSeconds
                seekable = Test-Seekable $playback $timeline
                browser = Test-BrowserSession $app
            }
        } catch {}
    }
}

function Get-DesktopSession($manager) {
    $preferred = $manager.GetCurrentSession()
    $paused = $null
    foreach ($session in (@($preferred) + @($manager.GetSessions()))) {
        if ($null -eq $session -or (Test-BrowserSession $session.SourceAppUserModelId)) {
            continue
        }
        $playback = $session.GetPlaybackInfo()
        if ($null -eq $playback) { continue }
        if ($playback.PlaybackStatus -eq 'Playing') { return $session }
        if ($null -eq $paused -and $playback.PlaybackStatus -eq 'Paused') { $paused = $session }
    }
    return $paused
}

try {
    $manager = Wait-WinRT ($managerType::RequestAsync()) $managerType
} catch {
    [Console]::WriteLine('{"error":"Windows media controls unavailable"}')
    exit 1
}

$lastMetadata = $null
$generation = 0
$artworkAttempts = 0
$nextArtworkAttempt = [DateTime]::MinValue
$seekQuietUntil = [DateTime]::MinValue
$nextSessionScan = [DateTime]::MinValue
. (Join-Path $PSScriptRoot 'amazon-music.ps1')
. (Join-Path $PSScriptRoot 'soundcloud.ps1')
$amazonStatus = ''
$amazonTrackKey = $null
$amazonCachedArtwork = $null
$amazonSampleAt = [DateTime]::MinValue
try {
while ($null -ne ($line = [Console]::ReadLine())) {
    $sessionSummary = @{}
    try {
        $command = ConvertFrom-Json -InputObject $line
        if ($command.amazonAction) {
            try {
                if ($command.amazonAction -eq 'connect' -and !$amazonPort) {
                    Start-AmazonMusic
                    $amazonStatus = 'Play a track in Amazon Music to activate the connection.'
                } elseif ($command.amazonAction -eq 'disconnect') {
                    Stop-AmazonMusicConnection
                    $amazonStatus = ''
                    $amazonCachedArtwork = $null
                    $amazonSampleAt = [DateTime]::MinValue
                }
            } catch {
                $amazonStatus = "Amazon Music: $($_.Exception.GetBaseException().Message)"
            }
        }
        $sessionSummary.amazonConnected = [bool]$amazonPort
        $sessionSummary.amazonStatus = $amazonStatus
        if ([DateTime]::UtcNow -ge $nextSessionScan -and $null -eq $command.target) {
            $nextSessionScan = [DateTime]::UtcNow.AddSeconds(2)
            $sessionSummary.sessions = @(Get-MediaSessionSummary $manager)
        }
        $session = Get-DesktopSession $manager
        $amazonRecentlyActive = ([DateTime]::UtcNow - $amazonSampleAt).TotalMilliseconds -lt 1600
        if ($amazonPort -and (!$session -or $session.SourceAppUserModelId -like 'AmazonMobileLLC.AmazonMusic*' -or
            ($amazonRecentlyActive -and $session.GetPlaybackInfo().PlaybackStatus -ne 'Playing'))) {
            $seekKey = if ($command.generation -eq $generation -and $null -ne $command.target) { $amazonTrackKey }
            $amazon = Get-AmazonMusicSample $seekKey $command.target $amazonCachedArtwork
            if ($amazon) {
                $identity = "AmazonMusicRenderer:$($amazon.key)"
                if ($lastMetadata -cne $identity) {
                    $generation++
                    $lastMetadata = $identity
                    $amazonCachedArtwork = $null
                }
                $amazonTrackKey = $amazon.key
                $amazonSampleAt = [DateTime]::UtcNow
                $sessionSummary.amazonStatus = ''
                $sample = @{
                    generation = $generation
                    app = 'Amazon Music'
                    source = 'amazon_music'
                    title = $amazon.title
                    credit = $amazon.credit
                    playing = $amazon.playing
                    seekable = $amazon.seekable
                    seekAccepted = $amazon.seekAccepted
                    rate = 1
                    duration = $amazon.duration
                    position = $amazon.position
                    artworkUrl = $amazon.artworkUrl
                }
                if ($amazon.artwork) {
                    $sample.artwork = $amazon.artwork
                    $amazonCachedArtwork = @{ key = $amazon.key; url = $amazon.artworkUrl }
                }
                [Console]::WriteLine((ConvertTo-Json -InputObject ($sample + $sessionSummary) -Compress))
                continue
            }
            if ($amazonConnectionError) {
                $sessionSummary.amazonStatus = "Amazon Music connection: $amazonConnectionError"
            }
            if ($amazonRecentlyActive) {
                $sessionSummary.amazonTransition = $true
                [Console]::WriteLine((ConvertTo-Json -InputObject $sessionSummary -Compress))
                continue
            }
        }
        $amazonTrackKey = $null
        $amazonCachedArtwork = $null
        if ($null -eq $session) {
            $lastMetadata = $null
            [Console]::WriteLine((ConvertTo-Json -InputObject $sessionSummary -Compress))
            continue
        }
        $app = $session.SourceAppUserModelId
        $media = Wait-WinRT ($session.TryGetMediaPropertiesAsync()) $mediaType
        $metadata = ConvertTo-Json -Compress -InputObject @($app, $media.Title, $media.Artist, $media.AlbumTitle, $media.TrackNumber)
        $changed = $lastMetadata -cne $metadata
        if ($changed) {
            $generation++
            $lastMetadata = $metadata
            $artworkAttempts = 0
            $nextArtworkAttempt = [DateTime]::MinValue
        }
        $playback = $session.GetPlaybackInfo()
        $timeline = $session.GetTimelineProperties()
        $seekable = Test-Seekable $playback $timeline
        if ($seekable -and $command.generation -eq $generation) {
            $ticks = $timeline.StartTime.Ticks + [long]($command.target * [TimeSpan]::TicksPerSecond)
            $ticks = [Math]::Max($timeline.MinSeekTime.Ticks, [Math]::Min($timeline.MaxSeekTime.Ticks, $ticks))
            try { $seekAccepted = Wait-WinRT ($session.TryChangePlaybackPositionAsync($ticks)) ([bool]) }
            catch { $seekAccepted = $false }
            $seekQuietUntil = [DateTime]::UtcNow.AddMilliseconds(500)
            $timeline = $session.GetTimelineProperties()
        }
        $playing = $playback.PlaybackStatus -eq 'Playing'
        $rate = if ($null -eq $playback.PlaybackRate) { 1 } else { $playback.PlaybackRate }
        $position = ($timeline.Position - $timeline.StartTime).TotalSeconds
        $duration = ($timeline.EndTime - $timeline.StartTime).TotalSeconds
        if ($duration -le 0 -and $app -like 'SoundcloudLtd.SoundCloud-MusicAudio_*!App') {
            $soundCloud = Get-SoundCloudTimeline ([string]$media.Title).Trim() ([string]$media.Artist).Trim()
            if ($soundCloud) {
                $position = $soundCloud.position
                $duration = $soundCloud.duration
            }
        }
        $sample = @{
            generation = $generation
            app = $app
            title = $media.Title
            credit = $media.Artist
            playing = $playing
            seekable = $seekable
            rate = $rate
            duration = $duration
            position = $position
        }
        if ($command.generation -eq $generation -and $null -ne $command.target) {
            $sample.seekAccepted = $seekable -and $seekAccepted
        }
        $now = [DateTime]::UtcNow
        if ($artworkAttempts -lt 4 -and $now -ge $nextArtworkAttempt -and
            $now -ge $seekQuietUntil -and $null -ne $media.Thumbnail) {
            $artworkAttempts++
            $nextArtworkAttempt = [DateTime]::UtcNow.AddSeconds(2)
            try {
                $stream = Wait-WinRT ($media.Thumbnail.OpenReadAsync()) $streamType
                $artwork = Read-ImageStream $stream
                if ($artwork) {
                    $sample.artwork = $artwork
                    $artworkAttempts = 4
                }
            } catch {}
        }
        [Console]::WriteLine((ConvertTo-Json -InputObject ($sample + $sessionSummary) -Compress))
    } catch {
        $lastMetadata = $null
        [Console]::WriteLine((ConvertTo-Json -InputObject $sessionSummary -Compress))
    }
}
} finally {
    Stop-AmazonMusicConnection
}
