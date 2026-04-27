I am working on creating a light weight appliction for helping me logging the working time

# Background

In my company, it's required everyone log the work time in 15m block on a jira. it's time consuming to log the time and upload them to jira so I want to create a little helper application for automating this part of the task 

# Logging time to Jira

it's mandatory to log the time within a jira ticket, for example if I worked 1 hour for task SFXS-1073, I need to log 1 hour time under SFXS-1073 with a note: Create PR and document changes (just for example)

# Some Requirement

1. should be able to call jira api and update jira automatically

2. Should reduce the effort for logging time, remembering ticket, time the work for tickets especially switching between work takes a lot of time 

3. Less stress, during the development we should take data base security into account, I don't want to corrupt our jira base 

4. cli, it would be nice to interact with the tool by cli, I am familiar with cli and would like to use it more

5. you are free to pick front and back end architecture, framework, I am open to all

6. give me a mvp first, I will add jira api key later for testing
