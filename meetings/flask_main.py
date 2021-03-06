"""
Revision Author: John Nemeth
Sources: python documentation, class material
DescriptiOn: main file for flask server
"""
import flask
from flask import render_template
from flask import request
from flask import url_for
import uuid
import json
import logging
# Date handling 
import arrow
# for interpreting local times
from dateutil import tz
# OAuth2  - Google library implementation for convenience
from oauth2client import client
# used in oauth2 flow
import httplib2
# Google API for services 
from apiclient import discovery
# used for email
import email.mime.text
import base64

# created modules
import times
import agenda
import db
import calfuncs
import gmailsend

###
# Globals
###
import config
if __name__ == "__main__":
    CONFIG = config.configuration()
else:
    CONFIG = config.configuration(proxied=True)

app = flask.Flask(__name__)
app.debug=CONFIG.DEBUG
app.logger.setLevel(logging.DEBUG)
app.secret_key=CONFIG.SECRET_KEY

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/gmail.send']
# for google oauth2 credentials
CLIENT_SECRET_FILE = CONFIG.GOOGLE_KEY_FILE
APPLICATION_NAME = 'meets project'

"""
###############################  Pages (routed from URLs)
"""
#########
@app.route("/")
@app.route("/index")
def index():
  app.logger.debug("Entering index")
  
  # to give users a welcome to app
  flask.g.homepage = True
  if 'begin_date' not in flask.session:
    init_session_values()
  return render_template('index.html')

#########
# huge choose route that deals constantly with valid credentials
#   and auth routing. all input to webpage goes through here and
#   if request method is post, grab eventlist.
@app.route("/choose", methods=['POST', 'GET'])
def choose():
    app.logger.debug("Checking credentials for Google calendar access")
    credentials = valid_credentials()
    if not credentials:
      app.logger.debug("Redirecting to authorization")
      return flask.redirect(flask.url_for('oauth2callback'))
    
    #get calendars before method check to use cal summary
    gcal_service = get_gcal_service(credentials)
    app.logger.debug("Returned from get_gcal_service")
    flask.g.calendars = list_calendars(gcal_service)
    
    # get list of cals that are owned by user
    flask.g.ownedcals = calfuncs.getOwnedCals(flask.g.calendars)
    
    # to check if user is owner of any created meetings
    flask.g.isowner = db.checkIsOwner(flask.g.ownedcals)
    
    # to check if should put 'list invited meetings' button in template
    flask.g.isinvited = db.checkIsInvited(flask.g.ownedcals)
    
    # request method is post and button pressed is to choose calendars
    if request.method == 'POST' and 'calchoose' in request.form:
        calendars = request.form.getlist('calendar')
        if not calendars:
            flask.flash('no calendars selected!')
            return render_template('index.html')

        # put selected cals in cookie for other routes
        flask.session['selected'] = calfuncs.getSelectedCals(calendars)
        # get selected cal summaries and ids
        calsummaries, calendarids = calfuncs.getIdsAndSums(flask.session['selected'])
        
        # get list of events
        events = getEvents(calendarids, calsummaries, credentials, gcal_service)

        # create list of days (contains 24hrs of freetime initially)
        daysList = agenda.getDayList(flask.session['begin_date'], flask.session['end_date'])

        # populate agenda with events (and split/modify freetimes)
        daysAgenda = agenda.populateDaysAgenda(daysList, events)

        # restrict blocks by timerange in new list 
        rangedAgenda = agenda.getEventsInRange(daysAgenda, flask.session['begin_time'], flask.session['end_time'])

        # create list of only free times
        flask.g.free = agenda.getFreeTimes(rangedAgenda)
    # request method is post and button pressed is to choose freetime
    elif request.method == 'POST' and 'ftchoose' in request.form:
        if 'freetimechosen' not in request.form:
            flask.flash("no freetime chosen!")
            return render_template('index.html')
        times = request.form['freetimechosen']
        times = times.split(',')
        flask.g.date = arrow.get(times[0]).format('YYYY-MM-DD')
        flask.g.start = times[0]
        flask.g.end = times[1]
    
    return render_template('index.html')

#########
# create record of new event and email it
@app.route("/create", methods=['POST'])
def create():
    app.logger.debug("Entering create route")
    if 'eventowner' not in request.form:
        flask.flash('no meeting owner chosen! meeting invitation not created.')
        return flask.redirect(flask.url_for("choose"))
    owner = request.form['eventowner']
    ownerparts= owner.split(',')
    starttime = request.form['timestart']
    endtime = request.form['timeend']
    title = request.form['title']
    date = request.form['date']
    desc = request.form['description']
    emails = request.form['emailinput']
    
    """
    ### for entering invitation into database ###
    """
    # place selected calendars into invitee list if isn't selected owner calendar
    invitees = []
    for calid, items in flask.session['selected'].items():
        if items[1] != 'owner':
            cal = {}
            cal['id'] = calid
            cal['status'] = 'pending'
            cal['summary'] = items[0]
            invitees.append(cal)
    
    # error check for unfilled, required fields
    if not title or not desc or not starttime or not endtime or not date:
        flask.flash("one of 5 required fields left empty! (times, date, title, and description")
    else:
        start = arrow.get(date + ' ' + starttime).replace(tzinfo=tz.tzlocal()).isoformat()
        end = arrow.get(date + ' ' + endtime).replace(tzinfo=tz.tzlocal()).isoformat() 
        db.enterinDB(title, desc, start, end, ownerparts[0], ownerparts[1], invitees)
        flask.flash("meeting invitation(s) successfully created!")
    
    """
    ### email operations kept in create route because of google auth complexity ###
    """
    # check if emails list entered
    if emails:
        # remove last comma from input
        emails = emails.split(',')
        emails = emails[0 : -1]
        credentials = valid_credentials()
        if not credentials:
            app.logger.debug("Redirecting to authorization")
            return flask.redirect(flask.url_for('oauth2callback'))
        # create gmail service
        gmailService = get_gmail_service(credentials)
        newdesc = gmailsend.appendMsgToHeader(start, end, title, desc, CONFIG.PORT)
        # send an email for every email entered
        for email in emails:
            message = gmailsend.createMessage(email, title, newdesc)
            gmailsend.sendMessage(gmailService, message)
            flask.flash("email successfully sent!")
    else:
        flask.flash('no emails were specified to receive the invitation!')
    
    return flask.redirect(flask.url_for("choose"))

########
# display meeting info as person who setup the meeting
@app.route("/meetings", methods=['POST'])
def meetings():
    app.logger.debug("Entering meetings route")
    
    # in case of redirect
    if 'calsinfo' not in request.form:
        flask.g.ownedcals = flask.session['ownedcals']
    else:
        calsinfo = request.form.getlist('calsinfo')
        flask.g.ownedcals = calfuncs.getCalsFromHTML(calsinfo)
        flask.session['ownedcals'] = flask.g.ownedcals
    flask.g.ownedmeetings = db.getOwnedMeetings(flask.g.ownedcals)
    
    app.logger.debug("Leaving meetings route")
    return render_template('meetings.html')

########
# display meeting invitations as invitee (can only see owned invitations)
@app.route("/invites", methods=['POST', 'GET'])
def invites():
    app.logger.debug("entering invited route")

    # in case of redirect
    if 'calsinfo' not in request.form:
        flask.g.ownedcals = flask.session['ownedcals']
    else:
        calsinfo = request.form.getlist('calsinfo')
        flask.g.ownedcals = calfuncs.getCalsFromHTML(calsinfo)
        flask.session['ownedcals'] = flask.g.ownedcals
    flask.g.ownedinvites = db.getInvitedMeetings(flask.g.ownedcals)

    app.logger.debug("leaving invited route")
    return render_template('invites.html')

######
# route to set invitation to accepted
@app.route("/accept", methods=['POST'])
def accept():
    app.logger.debug("entering accept route")
    idsDict = calfuncs.splitIds(request.form.get('accept'))
    # change relevant invite to 'accepted'
    db.modifyStatus(idsDict, 'accepted')
    # check if all invites 'accepted', changes meeting status to 'confirmed'
    db.checkMeetingConfirm(idsDict)
    
    app.logger.debug("exiting accept route")
    return flask.redirect('invites')
    
#####
# route to set invitation to rejected
@app.route("/reject", methods=['POST'])
def reject():
    app.logger.debug("enter reject route")
    idsDict = calfuncs.splitIds(request.form.get('reject'))
    # change relevant invite to 'rejected'
    db.modifyStatus(idsDict, 'rejected')
    
    app.logger.debug("exit reject route")
    return flask.redirect('invites')


"""
###################### gmail service object
"""
# retrieve the service object for google calendar
def get_gmail_service(credentials):
  app.logger.debug("Entering get_gmail_service")
  http_auth = credentials.authorize(httplib2.Http())
  service = discovery.build('gmail', 'v1', http=http_auth)
  app.logger.debug("Returning service")
  return service

"""
################### calendar service object
"""
# retrieve the service object for google calendar
def get_gcal_service(credentials):
  app.logger.debug("Entering get_gcal_service")
  http_auth = credentials.authorize(httplib2.Http())
  service = discovery.build('calendar', 'v3', http=http_auth)
  app.logger.debug("Returning service")
  return service


"""
################### auth procedures
"""

#checks for valid credentials
def valid_credentials():
   
    # will eventually redirect to oauth2callback
    if 'credentials' not in flask.session:
      return None
    # will convert
    credentials = client.OAuth2Credentials.from_json(
        flask.session['credentials'])
    if (credentials.invalid or
        credentials.access_token_expired):
      return None
    return credentials

# oauth2callback directs to google for valid credentials
@app.route('/oauth2callback')
def oauth2callback():
  app.logger.debug("Entering oauth2callback")
  flow =  client.flow_from_clientsecrets(
      CLIENT_SECRET_FILE,
      scope= SCOPES,
      redirect_uri=flask.url_for('oauth2callback', _external=True))
  
  app.logger.debug("Got flow")
  if 'code' not in flask.request.args:
    app.logger.debug("Code not in flask.request.args")
    auth_uri = flow.step1_get_authorize_url()
    return flask.redirect(auth_uri)
  else:
    app.logger.debug("Code was in flask.request.args")
    auth_code = flask.request.args.get('code')
    credentials = flow.step2_exchange(auth_code)
    flask.session['credentials'] = credentials.to_json()
    app.logger.debug("Got credentials")
    return flask.redirect(flask.url_for('choose'))

#####
# routes to affect things on page
#####

@app.route('/setrange', methods=['POST'])
def setrange():
    """
    User chose a date range with the bootstrap daterange
    widget.
    """
    if 'daterange' not in request.form:
        return flask.redirect(flask.url_for("choose"))
    app.logger.debug("Entering setrange")  
    #flask.flash("Setrange gave us '{}'".format(
    #  request.form.get('daterange')))
    daterange = request.form.get('daterange')
    flask.session['daterange'] = daterange
    daterange_parts = daterange.split()
    flask.session['begin_date'] = interpret_date(daterange_parts[0])
    flask.session['end_date'] = interpret_date(daterange_parts[2])
    app.logger.debug("Setrange parsed {} - {}  dates as {} - {}".format(
      daterange_parts[0], daterange_parts[1], 
      flask.session['begin_date'], flask.session['end_date']))
    end = arrow.get(flask.session['end_date'])
    end = end.shift(minutes=-1)
    flask.session['end_date'] = end.ceil('day').isoformat()
    flask.session["begin_time"] = interpret_time(request.form.get('timestart'))
    flask.session["end_time"] = interpret_time(request.form.get('timeend'))
    return flask.redirect(flask.url_for("choose"))

####
#  Initialize session variables 
####

# must be run in app context. can't call from main
def init_session_values():
    # Default date span = tomorrow to 1 week from now
    now = arrow.now('local')
    tomorrow = now.replace(days=+1)
    nextweek = now.replace(days=+7)
    flask.session["begin_date"] = tomorrow.floor('day').isoformat()
    flask.session["end_date"] = nextweek.ceil('day').isoformat()
    flask.session["daterange"] = "{} - {}".format(
        tomorrow.format("MM/DD/YYYY"),
        nextweek.format("MM/DD/YYYY"))
    # Default time span each day, 8 to 5
    flask.session["begin_time"] = interpret_time("9am")
    flask.session["end_time"] = interpret_time("5pm")

def interpret_time( text ):
    """
    Read time in a human-compatible format and
    interpret as ISO format with local timezone.
    May throw exception if time can't be interpreted. In that
    case it will also flash a message explaining accepted formats.
    """
    app.logger.debug("Decoding time '{}'".format(text))
    time_formats = ["ha", "h:mma",  "h:mm a", "H:mm"]
    try: 
        as_arrow = arrow.get(text, time_formats).replace(tzinfo=tz.tzlocal())
        as_arrow = as_arrow.replace(year=2016) #HACK see below
        app.logger.debug("Succeeded interpreting time")
    except:
        app.logger.debug("Failed to interpret time")
        flask.flash("Time '{}' didn't match accepted formats 13:30 or 1:30pm"
              .format(text))
        raise
    return as_arrow.isoformat()
    #HACK #Workaround
    # isoformat() on raspberry Pi does not work for some dates
    # far from now.  It will fail with an overflow from time stamp out
    # of range while checking for daylight savings time.  Workaround is
    # to force the date-time combination into the year 2016, which seems to
    # get the timestamp into a reasonable range. This workaround should be
    # removed when Arrow or Dateutil.tz is fixed.
    # FIXME: Remove the workaround when arrow is fixed (but only after testing
    # on raspberry Pi --- failure is likely due to 32-bit integers on that platform)


def interpret_date( text ):
    """
    Convert text of date to ISO format used internally,
    with the local time zone.
    """
    try:
      as_arrow = arrow.get(text, "MM/DD/YYYY").replace(
          tzinfo=tz.tzlocal())
    except:
        flask.flash("Date '{}' didn't fit expected format 12/31/2001")
        raise
    return as_arrow.isoformat()

def next_day(isotext):
    """
    ISO date + 1 day (used in query to Google calendar)
    """
    as_arrow = arrow.get(isotext)
    return as_arrow.replace(days=+1).isoformat()

"""
###################  Functions (NOT pages) that return some information
"""
#######
# Get events, to get events from calendars chosen in template
def getEvents(calid, calsum, credentials, service):
    app.logger.debug("Entering getEvents")
    eventsbycalendar = {}
    for count, ids in enumerate(calid):
        events = service.events().list(calendarId=ids,
                                       singleEvents=True,
                                       orderBy='startTime',
                                       timeMin=flask.session['begin_date'],
                                       timeMax=flask.session['end_date']).execute()
        eventclasslist = []
        for event in events['items']:
            if 'transparency' not in event:
                starttime = event['start']
                endtime = event['end']
                
                #to determine whether is all day event or if times specified
                if 'dateTime' in starttime:
                    start = starttime['dateTime']
                    end = endtime['dateTime']
                else:
                    start = starttime['date']
                    end = endtime['date']
                if 'summary' in event:
                    summ = event['summary']
                else:
                    summ = 'no title'
                eventclass = times.timeblock(start, end, 'event', summ)
                
                # to split events if they include multiple days
                passedEvent = agenda.fixEventTimes(eventclass)
                try:
                    for aEvent in passedEvent:
                        eventclasslist.append(aEvent)
                except TypeError:
                    eventclasslist.append(passedEvent)
        
        eventsbycalendar[calsum[count]] = eventclasslist
        app.logger.debug("Leaving getEvents")
    return eventsbycalendar

#####
# determine a list of cals from list obtained from google  
def list_calendars(service):
    app.logger.debug("Entering list_calendars")  
    calendar_list = service.calendarList().list().execute()["items"]
    result = [ ]
    for cal in calendar_list:
        kind = cal["kind"]
        id = cal["id"]
        if "description" in cal: 
            desc = cal["description"]
        else:
            desc = "(no description)"
        
        summary = cal["summary"]
        # Optional binary attributes with False as default
        selected = ("selected" in cal) and cal["selected"]
        primary = ("primary" in cal) and cal["primary"]
        # attributes to determine if owner of calendar
        accessrole = cal["accessRole"]
        
        result.append(
          { "kind": kind,
            "id": id,
            "summary": summary,
            "selected": selected,
            "primary": primary,
            "accessrole": accessrole
            })
        app.logger.debug("Leaving list_calendars")
    return sorted(result, key=cal_sort_key)


def cal_sort_key( cal ):
    """
    Sort key for the list of calendars:  primary calendar first,
    then other selected calendars, then unselected calendars.
    (" " sorts before "X", and tuples are compared piecewise)
    """
    if cal["selected"]:
       selected_key = " "
    else:
       selected_key = "X"
    if cal["primary"]:
       primary_key = " "
    else:
       primary_key = "X"
    return (primary_key, selected_key, cal["summary"])


#################
#
# Functions used within the templates
#
#################

@app.template_filter( 'fmtdate' )
def format_arrow_date( date ):
    try: 
        normal = arrow.get( date )
        return normal.format("ddd MM/DD/YY")
    except:
        return "(bad date)"

@app.template_filter( 'fmttime' )
def format_arrow_time( time ):
    try:
        normal = arrow.get( time )
        return normal.format("HH:mm")
    except:
        return "(bad time)"

@app.template_filter('fmtfreetime')
def format_free_time(time):
    try:
        freetime = arrow.get(time)
        return freetime.format('h:mm a')
    except:
        return "(bad time)"
 
    
#############


if __name__ == "__main__":
  # App is created above so that it will
  # exist whether this is 'main' or not
  # (e.g., if we are running under green unicorn)
  app.run(port=CONFIG.PORT,host="0.0.0.0")
    
