@echo off
rem Builds the ANVI Android app: mobile\build\app\outputs\flutter-apk\app-release.apk
rem Avast's HTTPS scanning breaks Gradle downloads, so Java uses a copy of its
rem certificate list with the Avast root added (.anvi\java-truststore).
cd /d "%~dp0mobile"
set "PATH=C:\Users\gkous\Desktop\flutter\flutter\bin;%PATH%"
if exist "%~dp0.anvi\java-truststore" set "JAVA_TOOL_OPTIONS=-Djavax.net.ssl.trustStore=%~dp0.anvi\java-truststore -Djavax.net.ssl.trustStorePassword=changeit"
call flutter build apk --release
if errorlevel 1 exit /b 1
copy /y "build\app\outputs\flutter-apk\app-release.apk" "%~dp0ANVI.apk" >nul
echo.
echo Built: %~dp0ANVI.apk
