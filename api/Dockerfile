FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY *.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app

# Dev stage: hot-reloads on source changes via `dotnet watch`.
# Used locally (see docker-compose.yml `target: dev`); not part of the
# default build, so `docker build .` with no --target still produces the
# production image below.
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS dev
WORKDIR /src
EXPOSE 5080
ENV ASPNETCORE_URLS=http://+:5080
ENTRYPOINT ["dotnet", "watch", "run", "--no-launch-profile", "--urls", "http://+:5080"]

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .
EXPOSE 5080
ENV ASPNETCORE_URLS=http://+:5080
ENTRYPOINT ["dotnet", "EnterpriseAiWorkspace.Api.dll"]
