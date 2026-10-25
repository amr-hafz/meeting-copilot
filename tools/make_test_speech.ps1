param(
    [string]$Out = (Join-Path $PSScriptRoot "..\samples\meeting_test.wav")
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path (Split-Path $Out) | Out-Null

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = 0
$synth.Volume = 80

$ssml = @"
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
  Hi everyone, thanks for joining on such short notice. <break time="800ms"/>
  The main thing I want to cover today is the migration timeline. <break time="700ms"/>
  The client asked us to move the delivery date to the fifteenth of next month. <break time="900ms"/>
  Can you prepare a short summary of the risks before Thursday? <break time="800ms"/>
  And please include the budget estimate in the same document. <break time="900ms"/>
  Do you have any questions before we wrap up? <break time="800ms"/>
</speak>
"@

$synth.SetOutputToWaveFile($Out)
$synth.SpeakSsml($ssml)
$synth.Dispose()

Write-Output "Wrote $Out"
